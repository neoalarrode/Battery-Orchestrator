"""
Motor de una zona de Climate — version sincrona, MQTT, del antiguo
`ClimateOrchestratorZone` (custom_component). Puerto fiel: misma logica de
decision, mismos nombres de metodo (sin el prefijo `async_`/`_async_`,
todo sincrono ahora), mismo criterio en cada rama — solo cambia COMO habla
con HA (WebSocket en vez de acceso directo a `hass`) y COMO se expone la
entidad (MQTT Discovery en vez de ClimateEntity nativa, ver
mqtt_climate.py).

Diferencias deliberadas frente al original:
  - Sin asyncio: todo el codigo de Climate Orchestrator era `async def`
    porque HA Core lo exige; aqui no hace falta, Home Orchestrator ya es
    sincrono/con hilos (igual que el resto de Battery).
  - Sin RestoreEntity: la persistencia de estado (preset activo, consignas
    manuales, aprendizajes de sobreimpulso...) vive en config_store, bajo
    el namespace de este plugin — ver zone_state.py.
  - `self.hass.states.get(...)` -> `self.ws.get_state(...)`;
    `self.hass.services.async_call(...)` -> `self.ws.call_service(...)`.
  - `self.async_write_ha_state()` -> `self.mqtt.publish_state(...)` (ver
    mqtt_climate.py) sobre topics de estado MQTT Discovery.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from . import ema as ema_module, grid_signal, outdoor, power_model, presets as presets_module, scheduler, thermal_model, window_algorithm
from .const import (
    CONF_AUTO_WINDOW_DETECTION,
    CONF_CLIMATE_ENTITIES,
    CONF_COOL_SWITCHES,
    CONF_CURRENT_TEMP_SENSOR,
    CONF_DEADBAND,
    CONF_DOOR_WINDOW_ENTITIES,
    CONF_DRY_HUMIDITY_THRESHOLD,
    CONF_FORECAST_REFRESH_MINUTES,
    CONF_HEAT_SWITCHES,
    CONF_HISTORY_DAYS_FOR_INERTIA,
    CONF_HUMIDIFIER_ENTITIES,
    CONF_HUMIDITY_SENSOR,
    CONF_ACTUATOR_POWER,
    CONF_HOME_POWER_SENSOR,
    CONF_MAX_POWER_W,
    CONF_MAX_TEMP,
    CONF_MIN_OFF_SECONDS,
    CONF_MIN_ON_SECONDS,
    CONF_MIN_TEMP,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_PRESENCE_ENTITIES,
    CONF_PRESENCE_PRESET,
    CONF_AWAY_PRESET,
    CONF_PRESETS_TEXT,
    CONF_PRIORITY,
    CONF_SIMULATE,
    CONF_TARGET_HUMIDITY,
    CONF_TPI_CYCLE_MINUTES,
    CONF_WEATHER_ENTITY,
    DEFAULT_DEADBAND,
    DEFAULT_DRY_HUMIDITY_THRESHOLD,
    DEFAULT_FORECAST_REFRESH_MINUTES,
    DEFAULT_HISTORY_DAYS_FOR_INERTIA,
    DEFAULT_MAX_HUMIDITY,
    DEFAULT_MAX_POWER_W,
    DEFAULT_MAX_TEMP,
    DEFAULT_MIN_HUMIDITY,
    DEFAULT_MIN_OFF_SECONDS,
    DEFAULT_MIN_ON_SECONDS,
    DEFAULT_MIN_TEMP,
    DEFAULT_OUTDOOR_HORIZON_HOURS,
    DEFAULT_TARGET_HUMIDITY,
    DEFAULT_TPI_CYCLE_MINUTES,
)

_LOGGER = logging.getLogger("climate.zone_runner")

TEMP_EMA_HALFLIFE_SECONDS = 120
STALE_SENSOR_HARD_TIMEOUT_SECONDS = 5400  # 90 min
WRITE_MIN_INTERVAL_SECONDS = 20
MODEL_RECOMPUTE_MIN_INTERVAL_SECONDS = 21600  # 6 h
TEMP_SEND_TOLERANCE_DEG = 0.1
HUMIDITY_SEND_TOLERANCE_PCT = 1
EQUIPMENT_FAILURE_DETECTION_MINUTES = 30
EQUIPMENT_FAILURE_MIN_DELTA_DEG = 0.3
OVERSHOOT_STRIKES_THRESHOLD = 2

FAN_MODE_URGENT_KEYWORDS = ("high", "max", "turbo", "strong", "fast", "boost")
FAN_MODE_GENTLE_KEYWORDS = ("low", "quiet", "silent", "eco", "min", "sleep")

_ACTION_MAP = {"heat": "heating", "cool": "cooling", "idle": "idle", "dry": "drying", "fan_only": "fan"}
_PASSTHROUGH_MODES = {"dry": "dry", "fan_only": "fan_only"}  # hvac_mode -> action, en este puerto ambos son el mismo string


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _pick_fan_mode(fan_modes: list[str], urgent: bool, manual: str | None) -> str | None:
    if not fan_modes:
        return None
    if manual and manual in fan_modes:
        return manual
    keywords = FAN_MODE_URGENT_KEYWORDS if urgent else FAN_MODE_GENTLE_KEYWORDS
    for mode in fan_modes:
        if any(k in mode.lower() for k in keywords):
            return mode
    for mode in fan_modes:
        if "auto" in mode.lower():
            return mode
    return None


def zone_stagger_seconds(zone_id: str, refresh_minutes: int) -> float:
    digest = hashlib.sha1(zone_id.encode()).hexdigest()
    fraction = int(digest[:8], 16) / 0xFFFFFFFF
    return fraction * refresh_minutes * 60


class ZoneRunner:
    """Una instancia por zona configurada. `ws` (ha_websocket.HAWebSocketClient)
    y `mqtt` (mqtt_climate.MqttClimateEntity, ver ese modulo) se inyectan
    desde fuera -- este runner no abre ninguna conexion el mismo.

    `tuya` (opcional): referencia al `TuyaPlugin` cargado, si lo hay (ver
    core_app.py, que la conecta tras cargar los plugins) -- permite que
    esta zona controle un dispositivo Tuya EN EL MISMO PROCESO, sin pasar
    por Home Assistant, escribiendo `tuya:<device_id>` (o
    `tuya:<device_id>:<indice_climate>` si el perfil del dispositivo tiene
    mas de un bloque `climates:`) en vez de un `climate.*` de HA en
    `climate_entities` -- misma lista, mismo campo de siempre, sin
    configuracion nueva que aprender."""

    def __init__(self, zone_id: str, zone: dict, ws, mqtt, all_zones: list[dict], state: dict | None = None, tuya=None) -> None:
        self.zone_id = zone_id
        self.zone = zone
        self.ws = ws
        self.mqtt = mqtt
        self.tuya = tuya
        self.all_zones = all_zones  # config de TODAS las zonas -- para power_model._other_zone_entities

        self._last_full_capability: set[str] = set()
        self._manual_fan_mode: str | None = None
        self._fan_mode: str | None = None
        self._fan_modes: list[str] | None = None

        capability = self._refresh_hvac_modes()
        self._capability_pending = not capability

        try:
            self._presets = presets_module.parse_presets(self.zone.get(CONF_PRESETS_TEXT, ""))
        except ValueError:
            self._presets = []
        self._preset_modes = [presets_module.PRESET_AUTO, presets_module.PRESET_MANUAL] + [p["name"] for p in self._presets]
        self._preset_mode = presets_module.PRESET_AUTO

        self._min_temp = float(self.zone.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        self._max_temp = float(self.zone.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP))
        self.hvac_mode = self._default_hvac_mode(capability)
        self.hvac_action = "off" if self.hvac_mode == "off" else "idle"
        self.current_temperature: float | None = None
        self.current_humidity: float | None = None

        self._min_humidity = DEFAULT_MIN_HUMIDITY
        self._max_humidity = DEFAULT_MAX_HUMIDITY
        self.target_humidity = float(self.zone.get(CONF_TARGET_HUMIDITY, DEFAULT_TARGET_HUMIDITY))

        self.target_temperature: float | None = None
        self.target_temperature_low: float | None = None
        self.target_temperature_high: float | None = None
        self.available = True

        self._outdoor_forecast: list[float] = []
        self._outdoor_now: float | None = None
        self._thermal_model: dict = {}
        self.reason = "sin calcular todavia"
        self._active_preset_name: str | None = None

        self._last_state_write_ts: float | None = None
        self._last_written_signature: tuple | None = None
        self._model_last_computed_ts: float | None = None

        self._manual_heat: float | None = None
        self._manual_cool: float | None = None

        self._switch_last_change: dict[str, tuple[str, datetime]] = {}
        self._tpi_cycle_start: dict[str, datetime] = {}
        self._last_heat_on_percent: float | None = None
        self._last_cool_on_percent: float | None = None
        self._delegate_deviations: dict[str, float] = {}
        self._delegate_last_active: dict[str, tuple[str, float]] = {}
        self._delegate_overshoot_strikes: dict[str, int] = {}
        self._delegate_needs_explicit_off: set[str] = set()
        self._last_active_hvac_mode: str | None = self.hvac_mode if self.hvac_mode != "off" else None

        self._temp_ema = ema_module.Ema(TEMP_EMA_HALFLIFE_SECONDS)
        self._sensor_stale = False
        self._window_detector = window_algorithm.WindowSlopeDetector()
        self._equipment_run: tuple[str, datetime, float] | None = None
        self._equipment_failure_suspected = False
        self._power_model: dict = {}

        if state:
            self._restore(state)

    # -------------------------------------------------- persistencia ------

    def _restore(self, state: dict) -> None:
        """Equivalente a `async_added_to_hass` de RestoreEntity -- `state`
        viene de config_store (ver zone_store.py), guardado la ultima vez
        que se persistio esta zona."""
        valid_modes = set(self.hvac_modes)
        if not self._capability_pending:
            saved_mode = state.get("hvac_mode")
            if saved_mode in valid_modes:
                self.hvac_mode = saved_mode
            if self.hvac_mode != "off":
                self._last_active_hvac_mode = self.hvac_mode

        saved_preset = state.get("preset_mode")
        if saved_preset in (self._preset_modes or []):
            self._preset_mode = saved_preset
        if saved_preset == presets_module.PRESET_MANUAL:
            self._manual_heat = state.get("manual_heat")
            self._manual_cool = state.get("manual_cool")

        restored_fan_mode = state.get("fan_mode")
        if restored_fan_mode and restored_fan_mode != "auto":
            self._manual_fan_mode = restored_fan_mode

        target_humidity = state.get("target_humidity")
        if target_humidity is not None:
            self.target_humidity = float(target_humidity)

        learned_off = state.get("delegate_needs_explicit_off")
        if isinstance(learned_off, list):
            declared = set(self.zone.get(CONF_CLIMATE_ENTITIES) or [])
            self._delegate_needs_explicit_off = {e for e in learned_off if e in declared}

    def to_persisted_state(self) -> dict:
        """Lo que se guarda en config_store en cada cambio que importa —
        ver zone_store.py."""
        return {
            "hvac_mode": self.hvac_mode,
            "preset_mode": self._preset_mode,
            "manual_heat": self._manual_heat,
            "manual_cool": self._manual_cool,
            "fan_mode": self._manual_fan_mode,
            "target_humidity": self.target_humidity,
            "delegate_needs_explicit_off": sorted(self._delegate_needs_explicit_off),
        }

    def watched_entities(self) -> set[str]:
        """Sensores que este runner necesita escuchar por el WebSocket
        reactivo — ver ha_websocket.py set_watched_entities."""
        watched = {e for e in [
            self.zone.get(CONF_CURRENT_TEMP_SENSOR),
            self.zone.get(CONF_OUTDOOR_TEMP_SENSOR),
            self.zone.get(CONF_HUMIDITY_SENSOR),
            *(self.zone.get(CONF_PRESENCE_ENTITIES) or []),
            *(self.zone.get(CONF_DOOR_WINDOW_ENTITIES) or []),
            *(self.zone.get(CONF_CLIMATE_ENTITIES) or []),
            *(self.zone.get(CONF_HUMIDIFIER_ENTITIES) or []),
            *(c.get("sensor") for c in (self.zone.get(CONF_ACTUATOR_POWER) or {}).values() if c.get("sensor")),
            grid_signal.GRID_SIGNAL_ENTITY_ID,
        ] if e}
        return watched

    # ---------------------------------------------------- estado extra ----

    def extra_attributes(self) -> dict:
        zone_power_w, zone_power_breakdown = self._zone_power_w()
        return {
            "reason": self.reason,
            "active_preset": self._active_preset_name,
            "priority": self.zone.get(CONF_PRIORITY),
            "simulate": self.zone.get(CONF_SIMULATE, True),
            "thermal_model_reliable": self._thermal_model.get("reliable", False),
            "heating_rate_deg_h": round(self._thermal_model.get("heating_rate_deg_h", 0) or 0, 2),
            "cooling_rate_deg_h": round(self._thermal_model.get("cooling_rate_deg_h", 0) or 0, 2),
            "idle_loss_coeff": round(self._thermal_model.get("idle_loss_coeff", 0) or 0, 3),
            "retention": scheduler.retention_label(self._thermal_model.get("idle_loss_coeff")) if self._thermal_model.get("reliable") else "sin datos todavía",
            "outdoor_now": self._outdoor_now,
            "outdoor_forecast": [round(t, 1) for t in self._outdoor_forecast] if self._outdoor_forecast else [],
            "delegate_temperature_deviations": dict(self._delegate_deviations),
            "delegate_needs_explicit_off": sorted(self._delegate_needs_explicit_off),
            "sensor_stale": self._sensor_stale,
            "window_slope_deg_h": self._window_detector.slope_deg_h,
            "equipment_failure_suspected": self._equipment_failure_suspected,
            "zone_power_w": zone_power_w,
            "zone_power_breakdown": zone_power_breakdown,
            "climate_orchestrator_zone": True,
            **{f"grid_{k}": v for k, v in grid_signal.read(self.ws).items() if k != "forecast"},
            "tpi_heat_on_percent": self._last_heat_on_percent,
            "tpi_cool_on_percent": self._last_cool_on_percent,
        }

    # ------------------------------------------------------ capacidad -----

    def _compute_capability(self) -> set[str]:
        capability: set[str] = set()
        if self.zone.get(CONF_HEAT_SWITCHES):
            capability.add("heat")
        if self.zone.get(CONF_COOL_SWITCHES):
            capability.add("cool")
        for entity_id in self.zone.get(CONF_CLIMATE_ENTITIES) or []:
            try:
                state = self.ws.get_state(entity_id)
            except Exception:
                state = None
            supported = ((state or {}).get("attributes") or {}).get("hvac_modes") or []
            for mode in ("heat", "cool", *_PASSTHROUGH_MODES.values()):
                if mode in supported:
                    capability.add(mode)
        if self.zone.get(CONF_HUMIDIFIER_ENTITIES):
            capability.add("humidify")
        return capability

    def _refresh_hvac_modes(self) -> set[str]:
        capability = self._compute_capability()
        self._last_full_capability = capability

        modes = ["off"]
        if {"heat", "cool"} <= capability:
            modes.append("heat_cool")
        if "heat" in capability:
            modes.append("heat")
        if "cool" in capability:
            modes.append("cool")
        for hvac_mode, name in _PASSTHROUGH_MODES.items():
            if name in capability:
                modes.append(hvac_mode)
        self.hvac_modes = modes

        fan_modes = self._available_fan_modes()
        if fan_modes:
            self._fan_modes = fan_modes
            self._fan_mode = self._manual_fan_mode if self._manual_fan_mode in fan_modes else "auto"
        else:
            self._fan_modes = None
            self._fan_mode = None

        self.supports_humidify = "humidify" in capability
        self.supports_range = {"heat", "cool"} <= capability
        return capability

    def _available_fan_modes(self) -> list[str]:
        ordered = ["auto"]
        seen = {"auto"}
        for entity_id in self.zone.get(CONF_CLIMATE_ENTITIES) or []:
            try:
                state = self.ws.get_state(entity_id)
            except Exception:
                state = None
            if state is None:
                continue
            for m in (state.get("attributes") or {}).get("fan_modes") or []:
                if m not in seen:
                    ordered.append(m)
                    seen.add(m)
        return ordered if len(ordered) > 1 else []

    def _reconcile_hvac_mode(self, capability: set[str]) -> None:
        if self._capability_pending and capability:
            self._capability_pending = False
            self.hvac_mode = self._default_hvac_mode(capability)

    @staticmethod
    def _default_hvac_mode(capability: set[str]) -> str:
        if {"heat", "cool"} <= capability:
            return "heat_cool"
        if "cool" in capability:
            return "cool"
        if "heat" in capability:
            return "heat"
        return "off"

    def _effective_capability(self) -> str:
        return {"heat": "heat", "cool": "cool", "heat_cool": "heat_cool", **_PASSTHROUGH_MODES}.get(self.hvac_mode, "none")

    # --------------------------------------------------------- reactivo ---

    def handle_reactive_event(self) -> None:
        if self._capability_pending:
            capability = self._refresh_hvac_modes()
            self._reconcile_hvac_mode(capability)
        self.decide_and_act()

    def handle_forecast_refresh(self) -> None:
        self.refresh_forecast()

    # ------------------------------------------------------ lecturas HA ---

    @staticmethod
    def _is_tuya_ref(entity_id: str) -> bool:
        return entity_id.startswith("tuya:")

    def _resolve_tuya_handle(self, entity_id: str):
        """`entity_id` con forma `tuya:<device_id>` o
        `tuya:<device_id>:<indice_climate>` -> TuyaClimateHandle, o None si
        no hay plugin Tuya cargado, o ese dispositivo/indice no existe
        todavia (p.ej. Tuya aun arrancando, o se desinstalo) -- llamar
        SOLO cuando `_is_tuya_ref(entity_id)` ya es True; un None aqui
        significa "no disponible ahora mismo", nunca "no era una
        referencia Tuya" (ver _call_climate_service/_get_state, que no
        deben caer a ws.call_service con un entity_id que no es de HA)."""
        if self.tuya is None:
            return None
        parts = entity_id.split(":", 2)
        device_id = parts[1] if len(parts) > 1 else ""
        index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        try:
            return self.tuya.climate_handle(device_id, index)
        except Exception:
            return None

    def _call_climate_service(self, entity_id: str, service: str, service_data: dict | None = None) -> None:
        """Punto UNICO de salida para las ordenes de un actuador climate.*
        -- si `entity_id` es una referencia Tuya, se resuelve EN EL MISMO
        PROCESO contra `TuyaClimateHandle` en vez de pasar por
        `ws.call_service`. `set_fan_mode` no tiene equivalente en
        TuyaClimateHandle todavia (perfiles Tuya con fan_dp propio) -- se
        ignora en silencio en vez de fallar, igual que si el propio
        dispositivo no soportase esa orden."""
        if self._is_tuya_ref(entity_id):
            handle = self._resolve_tuya_handle(entity_id)
            if handle is None:
                return  # Tuya no disponible ahora mismo -- nunca se manda esto a ws.call_service
            data = service_data or {}
            if service == "set_hvac_mode" and "hvac_mode" in data:
                handle.set_hvac_mode(data["hvac_mode"])
            elif service == "set_temperature" and "temperature" in data:
                handle.set_temperature(data["temperature"])
            return
        self.ws.call_service("climate", service, service_data=service_data or {}, target={"entity_id": entity_id})

    def _get_state(self, entity_id: str) -> dict | None:
        if self._is_tuya_ref(entity_id):
            handle = self._resolve_tuya_handle(entity_id)
            if handle is None or not handle.available:
                return None
            return {
                "state": handle.hvac_mode,
                "attributes": {
                    "current_temperature": handle.current_temperature,
                    "temperature": handle.target_temperature,
                    # Lista generica -- TuyaClimateHandle no expone hoy los
                    # modos reales del perfil (mode_map), asi que se ofrece
                    # el par minimo que _drive_climate_actuator necesita
                    # para decidir apagar/encender sin adivinar de mas.
                    "hvac_modes": ["off", "heat", "cool"],
                    "fan_mode": None,
                    "fan_modes": [],
                },
            }
        try:
            return self.ws.get_state(entity_id)
        except Exception:
            return None

    def _read_current_temp(self) -> float | None:
        sensor = self.zone.get(CONF_CURRENT_TEMP_SENSOR)
        if not sensor:
            return None
        state = self._get_state(sensor)
        now = _utcnow()

        if state is not None and state.get("state") not in ("unknown", "unavailable", None):
            raw = _safe_float(state.get("state"))
            if raw is not None:
                self._sensor_stale = False
                last_updated_raw = state.get("last_updated")
                last_updated = _utcnow()
                if last_updated_raw:
                    try:
                        last_updated = datetime.fromisoformat(str(last_updated_raw).replace("Z", "+00:00"))
                    except ValueError:
                        pass
                return self._temp_ema.update(raw, last_updated)

        age = self._temp_ema.age_seconds(now)
        if age is not None and age <= STALE_SENSOR_HARD_TIMEOUT_SECONDS:
            self._sensor_stale = True
            return self._temp_ema.value
        self._sensor_stale = False
        return None

    def _presence_now(self) -> bool | None:
        entities = self.zone.get(CONF_PRESENCE_ENTITIES) or []
        if not entities:
            return None
        known = []
        for e in entities:
            state = self._get_state(e)
            if state is None or state.get("state") in ("unknown", "unavailable", None):
                continue
            known.append(state.get("state") in ("on", "home"))
        return any(known) if known else None

    def _read_humidity_now(self) -> float | None:
        sensor = self.zone.get(CONF_HUMIDITY_SENSOR)
        if not sensor:
            return None
        state = self._get_state(sensor)
        if state is None or state.get("state") in ("unknown", "unavailable", None):
            return None
        return _safe_float(state.get("state"))

    def _smart_idle_action(self, current_temp: float | None, heat_target: float | None, deadband: float) -> tuple[str, str | None]:
        if "dry" in self._last_full_capability:
            humidity = self._read_humidity_now()
            threshold = float(self.zone.get(CONF_DRY_HUMIDITY_THRESHOLD, DEFAULT_DRY_HUMIDITY_THRESHOLD))
            heat_margin_ok = (
                heat_target is None or current_temp is None or current_temp >= heat_target + deadband
            )
            if humidity is not None and humidity >= threshold:
                if heat_margin_ok:
                    return "dry", f"humedad {humidity:.0f}% ≥ {threshold:.0f}%: deshumidificando en vez de apagar"
        if "fan_only" in self._last_full_capability:
            return "fan_only", "dentro de margen: ventilando en vez de apagar del todo"
        return "idle", None

    def _real_door_window_open(self) -> bool:
        for e in self.zone.get(CONF_DOOR_WINDOW_ENTITIES) or []:
            state = self._get_state(e)
            if state is not None and state.get("state") == "on":
                return True
        return False

    def _check_equipment_failure(self, action: str, current_temp: float, now: datetime) -> None:
        if action not in ("heat", "cool"):
            self._equipment_run = None
            self._equipment_failure_suspected = False
            return
        if self._equipment_run is None or self._equipment_run[0] != action:
            self._equipment_run = (action, now, current_temp)
            return

        run_action, start_ts, start_temp = self._equipment_run
        elapsed_min = (now - start_ts).total_seconds() / 60
        if elapsed_min < EQUIPMENT_FAILURE_DETECTION_MINUTES:
            return

        delta = current_temp - start_temp
        progressed = delta >= EQUIPMENT_FAILURE_MIN_DELTA_DEG if run_action == "heat" \
            else -delta >= EQUIPMENT_FAILURE_MIN_DELTA_DEG
        if progressed:
            self._equipment_run = (run_action, now, current_temp)
            self._equipment_failure_suspected = False
        elif not self._equipment_failure_suspected:
            self._equipment_failure_suspected = True
            _LOGGER.warning(
                "%s: lleva %d min pidiendo %s sin que la temperatura se mueva lo esperado "
                "(%.1f°C ahora, %.1f°C al empezar) — posible fallo del equipo",
                self.zone.get("name"), int(elapsed_min), run_action, current_temp, start_temp,
            )

    def _climate_actuators(self) -> list[str]:
        return (
            (self.zone.get(CONF_HEAT_SWITCHES) or [])
            + (self.zone.get(CONF_COOL_SWITCHES) or [])
            + (self.zone.get(CONF_CLIMATE_ENTITIES) or [])
        )

    def _actuator_active(self, entity_id: str) -> bool:
        state = self._get_state(entity_id)
        if state is None:
            return False
        if entity_id.startswith("climate."):
            return (state.get("attributes") or {}).get("hvac_action") in ("heating", "cooling")
        return state.get("state") == "on"

    def _actuator_power_w(self, entity_id: str) -> tuple[float | None, str]:
        config = (self.zone.get(CONF_ACTUATOR_POWER) or {}).get(entity_id) or {}
        sensor = config.get("sensor")
        if sensor:
            state = self._get_state(sensor)
            if state is not None and state.get("state") not in ("unknown", "unavailable", None):
                val = _safe_float(state.get("state"))
                if val is not None:
                    return val, "measured"
        learned = self._power_model.get(entity_id)
        if learned and learned.get("reliable") and learned.get("learned_power_w") is not None:
            return float(learned["learned_power_w"]), "learned"
        estimated = config.get("estimated_w")
        if estimated:
            return float(estimated), "estimated"
        return None, "none"

    def _zone_power_w(self) -> tuple[float | None, dict]:
        breakdown: dict[str, dict] = {}
        total = 0.0
        any_known = False
        for entity_id in self._climate_actuators():
            if not self._actuator_active(entity_id):
                continue
            watts, source = self._actuator_power_w(entity_id)
            if watts is None:
                continue
            breakdown[entity_id] = {"watts": watts, "source": source}
            total += watts
            any_known = True
        return (total if any_known else None), breakdown

    def _zone_estimated_power_w(self) -> float | None:
        total = 0.0
        any_known = False
        for entity_id in self._climate_actuators():
            watts, _source = self._actuator_power_w(entity_id)
            if watts is None:
                continue
            total += watts
            any_known = True
        return total if any_known else None

    def _preset_value(self, preset_name: str, side: str) -> float | None:
        """Consigna del preset — a diferencia del original (entidad
        number.* propia, ajustable desde Lovelace), aqui vive directa en
        el propio texto de presets de la zona (`self._presets`, ya
        parseado por presets.py). Las entidades number.* independientes
        son una mejora de UX pendiente de portar (ver Fase 2c), no
        bloquean el motor de decision."""
        for p in self._presets:
            if p["name"] == preset_name:
                return p.get(f"{side}_temp")
        return None

    # ---------------------------------------------------- previsión cara --

    def refresh_forecast(self) -> None:
        capability = self._refresh_hvac_modes()
        self._reconcile_hvac_mode(capability)

        weather_entity = self.zone.get(CONF_WEATHER_ENTITY, "")
        self._outdoor_forecast = outdoor.get_outdoor_forecast(
            self.ws, self.zone, weather_entity, DEFAULT_OUTDOOR_HORIZON_HOURS
        )
        self._outdoor_now = self._outdoor_forecast[0] if self._outdoor_forecast else None

        home_power_sensor = self.zone.get(CONF_HOME_POWER_SENSOR, "") or grid_signal.read(self.ws).get("home_power_sensor") or ""
        actuator_power = self.zone.get(CONF_ACTUATOR_POWER) or {}
        entities_to_learn = [
            e for e in self._climate_actuators()
            if not actuator_power.get(e, {}).get("sensor") and not actuator_power.get(e, {}).get("estimated_w")
        ]

        now_ts = _utcnow().timestamp()
        if not self._models_settled(entities_to_learn) or self._model_last_computed_ts is None or (
            now_ts - self._model_last_computed_ts >= MODEL_RECOMPUTE_MIN_INTERVAL_SECONDS
        ):
            self._thermal_model = thermal_model.get_model(
                self.ws, self.zone, int(self.zone.get(CONF_HISTORY_DAYS_FOR_INERTIA, DEFAULT_HISTORY_DAYS_FOR_INERTIA)),
                fallback=self._thermal_model,
            )
            self._power_model = power_model.get_power_model(
                self.ws, entities_to_learn, self.zone_id, self.all_zones, home_power_sensor,
                int(self.zone.get(CONF_HISTORY_DAYS_FOR_INERTIA, DEFAULT_HISTORY_DAYS_FOR_INERTIA)),
                fallback=self._power_model,
            ) if home_power_sensor and entities_to_learn else {}
            self._model_last_computed_ts = now_ts

        self.decide_and_act()

    def _models_settled(self, entities_to_learn: list[str]) -> bool:
        if not self._thermal_model.get("reliable"):
            return False
        return all(self._power_model.get(e, {}).get("reliable") for e in entities_to_learn)

    # ---------------------------------------------------- decision barata -

    def decide_and_act(self) -> None:
        if self._capability_pending:
            self.available = False
            self._maybe_publish_state()
            return

        current_temp = self._read_current_temp()
        self.current_temperature = current_temp
        humidity = self._read_humidity_now()
        self.current_humidity = round(humidity) if humidity is not None else None
        if current_temp is None:
            self.available = False
            self._maybe_publish_state()
            return
        self.available = True

        deadband = float(self.zone.get(CONF_DEADBAND, DEFAULT_DEADBAND))
        min_temp = float(self.zone.get(CONF_MIN_TEMP, DEFAULT_MIN_TEMP))
        max_temp = float(self.zone.get(CONF_MAX_TEMP, DEFAULT_MAX_TEMP))
        capability = self._effective_capability()
        wants_heat = capability in ("heat", "heat_cool")
        wants_cool = capability in ("cool", "heat_cool")

        preset_name, preset_reason = presets_module.resolve_active_preset_name(
            self._preset_mode, [p["name"] for p in self._presets],
            self.zone.get(CONF_PRESENCE_PRESET, ""), self.zone.get(CONF_AWAY_PRESET, ""),
            self._presence_now(),
        )
        self._active_preset_name = preset_name
        if preset_name == presets_module.PRESET_MANUAL:
            preset_heat = self._manual_heat if wants_heat else None
            preset_cool = self._manual_cool if wants_cool else None
        else:
            preset_heat = self._preset_value(preset_name, "heat") if wants_heat else None
            preset_cool = self._preset_value(preset_name, "cool") if wants_cool else None

        window_alert = False
        if self.zone.get(CONF_AUTO_WINDOW_DETECTION):
            window_alert = self._window_detector.update(current_temp, _utcnow(), wants_heat, wants_cool)

        real_door_open = self._real_door_window_open()
        urgent = False
        force_off = False

        if self.hvac_mode == "off":
            action = "idle"
            heat_target, cool_target = preset_heat, preset_cool
            self.reason = "apagado desde el termostato"
        elif real_door_open or window_alert:
            window_reason = "puerta/ventana abierta" if real_door_open else (
                f"posible ventana abierta (pendiente {self._window_detector.slope_deg_h:.1f}°C/h en contra "
                "de lo pedido, sin sensor dedicado)"
            )
            can_fan = "fan_only" in self._last_full_capability and (
                self.hvac_mode == "fan_only" or self.hvac_mode == self._default_hvac_mode(self._last_full_capability)
            )
            if can_fan:
                action = "fan_only"
                heat_target = cool_target = None
                self.reason = f"{window_reason}: ventilando (calor/frío en pausa)"
            else:
                action = "idle"
                heat_target, cool_target = preset_heat, preset_cool
                self.reason = f"{window_reason}: en pausa"
                force_off = True
        elif self.hvac_mode in _PASSTHROUGH_MODES:
            action = _PASSTHROUGH_MODES[self.hvac_mode]
            heat_target = cool_target = None
            self.reason = f"modo {action} fijado a mano desde el termostato"
        else:
            heat_target, cool_target = preset_heat, preset_cool
            grid = grid_signal.read(self.ws)
            action, decide_reason = scheduler.decide_action(
                current_temp=current_temp, heat_target=heat_target, cool_target=cool_target,
                priority=self.zone.get(CONF_PRIORITY, "confort"), deadband=deadband,
                min_temp=min_temp, max_temp=max_temp,
                outdoor_now=self._outdoor_now, outdoor_forecast=self._outdoor_forecast,
                heating_rate_deg_h=self._thermal_model.get("heating_rate_deg_h", 0.0),
                cooling_rate_deg_h=self._thermal_model.get("cooling_rate_deg_h", 0.0),
                idle_loss_coeff=self._thermal_model.get("idle_loss_coeff", 0.0),
                grid_tier=grid["tier"], solar_surplus_now_w=grid["solar_surplus_now_w"],
                zone_estimated_power_w=self._zone_estimated_power_w(),
                grid_forecast=grid["forecast"],
            )
            self.reason = f"{preset_reason} — {decide_reason}"
            urgent = "de seguridad de la zona" in decide_reason
            if action == "idle" and self.hvac_mode == self._default_hvac_mode(self._last_full_capability):
                smart_action, smart_reason = self._smart_idle_action(current_temp, heat_target, deadband)
                if smart_reason:
                    action = smart_action
                    self.reason += f" — {smart_reason}"

        if self._sensor_stale:
            self.reason += " — aviso: sensor externo sin lectura nueva, usando la última suavizada"

        now = _utcnow()
        self._check_equipment_failure(action, current_temp, now)

        max_power = self.zone.get(CONF_MAX_POWER_W) or 0
        if action in ("heat", "cool") and max_power > 0:
            already_active = self.hvac_action in ("heating", "cooling")
            zone_power, _breakdown = self._zone_power_w()
            if not already_active and zone_power is not None and zone_power >= float(max_power):
                self.reason += f" — pospuesto: potencia actual {zone_power:.0f}W ≥ máximo {float(max_power):.0f}W"
                action = "idle"

        climate_idle_keep = action == "idle" and not force_off and self.hvac_mode not in ("off", *_PASSTHROUGH_MODES)

        heat_on_percent = scheduler.tpi_on_percent(current_temp, heat_target, self._outdoor_now, heating=True) \
            if action == "heat" and heat_target is not None else None
        cool_on_percent = scheduler.tpi_on_percent(current_temp, cool_target, self._outdoor_now, heating=False) \
            if action == "cool" and cool_target is not None else None
        tpi_cycle_minutes = float(self.zone.get(CONF_TPI_CYCLE_MINUTES, DEFAULT_TPI_CYCLE_MINUTES))
        self._last_heat_on_percent, self._last_cool_on_percent = heat_on_percent, cool_on_percent

        self._update_target_attrs(heat_target, cool_target)
        target_for_actuator = heat_target if action == "heat" else cool_target if action == "cool" else (heat_target or cool_target)
        real_action = self._execute(
            action, target_for_actuator, capability, current_temp, deadband, climate_idle_keep, force_off=force_off,
            heat_on_percent=heat_on_percent, cool_on_percent=cool_on_percent, tpi_cycle_minutes=tpi_cycle_minutes,
            urgent=urgent,
        )
        self.hvac_action = "off" if self.hvac_mode == "off" else _ACTION_MAP.get(real_action, "idle")

        humidify_active = self.hvac_mode != "off" and not force_off
        self._drive_humidifiers(humidify_active)

        self._maybe_publish_state()

    def _maybe_publish_state(self) -> None:
        signature = (self.available, self.hvac_action, self.hvac_mode, self.reason)
        now_ts = _utcnow().timestamp()
        significant_change = signature != self._last_written_signature
        elapsed_enough = (
            self._last_state_write_ts is None
            or (now_ts - self._last_state_write_ts) >= WRITE_MIN_INTERVAL_SECONDS
        )
        if significant_change or elapsed_enough:
            self.mqtt.publish_state(self)
            self._last_state_write_ts = now_ts
            self._last_written_signature = signature

    def _update_target_attrs(self, heat_target: float | None, cool_target: float | None) -> None:
        if self.hvac_mode == "heat_cool":
            self.target_temperature = None
            self.target_temperature_low = heat_target
            self.target_temperature_high = cool_target
        else:
            self.target_temperature = heat_target if self.hvac_mode == "heat" else cool_target
            self.target_temperature_low = None
            self.target_temperature_high = None

    # ------------------------------------------------------- actuadores ---

    def _execute(
        self, action: str, target_temp: float | None, capability: str, current_temp: float | None,
        deadband: float, climate_idle_keep: bool, force_off: bool = False,
        heat_on_percent: float | None = None, cool_on_percent: float | None = None,
        tpi_cycle_minutes: float = DEFAULT_TPI_CYCLE_MINUTES, urgent: bool = False,
    ) -> str:
        simulate = bool(self.zone.get(CONF_SIMULATE, True))
        real_heat = real_cool = False
        real_other: str | None = None
        target_temp = target_temp if target_temp is not None else self.current_temperature or 20.0
        now = _utcnow()

        if capability in ("heat", "heat_cool"):
            if heat_on_percent is not None:
                desired_heat_on = self._tpi_desired_on("heat", heat_on_percent, tpi_cycle_minutes, now)
            else:
                desired_heat_on = False
                self._tpi_cycle_start.pop("heat", None)
            heat_force = force_off or (not desired_heat_on and action == "cool")
            for sw in self.zone.get(CONF_HEAT_SWITCHES) or []:
                if self._drive_switch(sw, desired_heat_on, simulate, force=heat_force):
                    real_heat = True

        if capability in ("cool", "heat_cool"):
            if cool_on_percent is not None:
                desired_cool_on = self._tpi_desired_on("cool", cool_on_percent, tpi_cycle_minutes, now)
            else:
                desired_cool_on = False
                self._tpi_cycle_start.pop("cool", None)
            cool_force = force_off or (not desired_cool_on and action == "heat")
            for sw in self.zone.get(CONF_COOL_SWITCHES) or []:
                if self._drive_switch(sw, desired_cool_on, simulate, force=cool_force):
                    real_cool = True

        for entity_id in self.zone.get(CONF_CLIMATE_ENTITIES) or []:
            if climate_idle_keep:
                result = self._drive_climate_idle(entity_id, current_temp, deadband, simulate)
            else:
                result = self._drive_climate_actuator(entity_id, action, target_temp, current_temp, simulate, urgent)
            if result == "heat":
                real_heat = True
            elif result == "cool":
                real_cool = True
            elif result in ("dry", "fan_only"):
                real_other = result

        if real_heat:
            return "heat"
        if real_cool:
            return "cool"
        if real_other:
            return real_other
        return "idle"

    def _drive_climate_actuator(
        self, entity_id: str, action: str, target_temp: float, current_temp: float | None, simulate: bool,
        urgent: bool = False,
    ) -> str:
        state = self._get_state(entity_id)
        attrs = (state or {}).get("attributes") or {}
        supported = list(attrs.get("hvac_modes") or [])
        can_do = action in ("heat", "cool", "dry", "fan_only") and action in supported

        if can_do:
            if action in ("heat", "cool"):
                self._delegate_last_active[entity_id] = (action, target_temp)
                self._delegate_overshoot_strikes[entity_id] = 0
            if not simulate:
                if state is None or state.get("state") != action:
                    self._call_climate_service(entity_id, "set_hvac_mode", {"hvac_mode": action})
                if action in ("heat", "cool"):
                    compensated = self._compensate_delegate_target(entity_id, state, target_temp, current_temp)
                    current_target = _safe_float(attrs.get("temperature"))
                    if current_target is None or abs(current_target - compensated) > TEMP_SEND_TOLERANCE_DEG:
                        self._call_climate_service(entity_id, "set_temperature", {"temperature": compensated})
                    self._drive_delegate_fan_mode(entity_id, state, urgent)
            elif action in ("heat", "cool"):
                self._compensate_delegate_target(entity_id, state, target_temp, current_temp)
            return action

        if not simulate and "off" in supported and state is not None and state.get("state") != "off":
            self._call_climate_service(entity_id, "set_hvac_mode", {"hvac_mode": "off"})
        return "idle"

    def _drive_delegate_fan_mode(self, entity_id: str, state: dict | None, urgent: bool) -> None:
        if state is None:
            return
        attrs = state.get("attributes") or {}
        fan_modes = list(attrs.get("fan_modes") or [])
        desired_fan = _pick_fan_mode(fan_modes, urgent, self._manual_fan_mode)
        if desired_fan and desired_fan != attrs.get("fan_mode"):
            self._call_climate_service(entity_id, "set_fan_mode", {"fan_mode": desired_fan})

    def _drive_climate_idle(self, entity_id: str, current_temp: float | None, deadband: float, simulate: bool) -> str:
        state = self._get_state(entity_id)
        attrs = (state or {}).get("attributes") or {}
        supported = list(attrs.get("hvac_modes") or [])
        last = self._delegate_last_active.get(entity_id)

        if entity_id not in self._delegate_needs_explicit_off and last is not None:
            last_mode, last_target = last
            if last_mode in supported:
                self._check_delegate_overshoot(entity_id, last_mode, last_target, current_temp, deadband)

        if entity_id in self._delegate_needs_explicit_off or last is None or last[0] not in supported:
            if not simulate and "off" in supported and state is not None and state.get("state") != "off":
                self._call_climate_service(entity_id, "set_hvac_mode", {"hvac_mode": "off"})
            return "idle"

        last_mode, last_target = last
        if not simulate:
            compensated = self._compensate_delegate_target(entity_id, state, last_target, current_temp)
            if state is None or state.get("state") != last_mode:
                self._call_climate_service(entity_id, "set_hvac_mode", {"hvac_mode": last_mode})
            current_target = _safe_float(attrs.get("temperature"))
            if current_target is None or abs(current_target - compensated) > TEMP_SEND_TOLERANCE_DEG:
                self._call_climate_service(entity_id, "set_temperature", {"temperature": compensated})
            self._drive_delegate_fan_mode(entity_id, state, urgent=False)
        else:
            self._compensate_delegate_target(entity_id, state, last_target, current_temp)
        return "idle"

    def _check_delegate_overshoot(
        self, entity_id: str, last_mode: str, last_target: float, current_temp: float | None, deadband: float
    ) -> None:
        if current_temp is None:
            return
        overshoot = (
            (last_mode == "heat" and current_temp > last_target + deadband) or
            (last_mode == "cool" and current_temp < last_target - deadband)
        )
        if not overshoot:
            self._delegate_overshoot_strikes[entity_id] = 0
            return
        strikes = self._delegate_overshoot_strikes.get(entity_id, 0) + 1
        self._delegate_overshoot_strikes[entity_id] = strikes
        if strikes >= OVERSHOOT_STRIKES_THRESHOLD:
            self._delegate_needs_explicit_off.add(entity_id)
            _LOGGER.info(
                "%s: %s se mantenia encendido mas alla de su consigna repetidas veces — "
                "a partir de ahora se apaga de verdad al llegar a la consigna",
                self.zone.get("name"), entity_id,
            )

    def _compensate_delegate_target(self, entity_id: str, state: dict | None, target_temp: float, current_temp: float | None) -> float:
        attrs = (state or {}).get("attributes") or {}
        delegate_temp = attrs.get("current_temperature")
        if delegate_temp is None or current_temp is None:
            self._delegate_deviations.pop(entity_id, None)
            return target_temp
        try:
            deviation = float(delegate_temp) - float(current_temp)
        except (TypeError, ValueError):
            self._delegate_deviations.pop(entity_id, None)
            return target_temp

        self._delegate_deviations[entity_id] = round(deviation, 2)
        compensated = target_temp + deviation
        min_t = attrs.get("min_temp")
        max_t = attrs.get("max_temp")
        try:
            if min_t is not None:
                compensated = max(compensated, float(min_t))
            if max_t is not None:
                compensated = min(compensated, float(max_t))
        except (TypeError, ValueError):
            pass
        return compensated

    def _tpi_desired_on(self, side: str, on_percent: float, cycle_minutes: float, now: datetime) -> bool:
        cycle_seconds = max(60.0, cycle_minutes * 60)
        start = self._tpi_cycle_start.get(side)
        if start is None or (now - start).total_seconds() >= cycle_seconds:
            start = now
            self._tpi_cycle_start[side] = start
        elapsed = (now - start).total_seconds()
        return elapsed < on_percent * cycle_seconds

    def _drive_switch(self, entity_id: str, desired_on: bool, simulate: bool, force: bool = False) -> bool:
        state = self._get_state(entity_id)
        current_on = state is not None and state.get("state") == "on"
        now = _utcnow()

        if current_on == desired_on:
            self._switch_last_change.setdefault(entity_id, ("on" if current_on else "off", now))
            return current_on

        last_state, last_change = self._switch_last_change.get(entity_id, (None, None))
        if not force and last_change is not None:
            min_seconds = self.zone.get(CONF_MIN_ON_SECONDS, DEFAULT_MIN_ON_SECONDS) if last_state == "on" \
                else self.zone.get(CONF_MIN_OFF_SECONDS, DEFAULT_MIN_OFF_SECONDS)
            if (now - last_change).total_seconds() < min_seconds:
                return current_on

        if not simulate:
            service = "turn_on" if desired_on else "turn_off"
            self.ws.call_service("switch", service, target={"entity_id": entity_id})
        self._switch_last_change[entity_id] = ("on" if desired_on else "off", now)
        return desired_on

    def _drive_humidifiers(self, active: bool) -> None:
        simulate = bool(self.zone.get(CONF_SIMULATE, True))
        for entity_id in self.zone.get(CONF_HUMIDIFIER_ENTITIES) or []:
            if simulate:
                continue
            state = self._get_state(entity_id)
            attrs = (state or {}).get("attributes") or {}
            if active:
                if state is None or state.get("state") != "on":
                    self.ws.call_service("humidifier", "turn_on", target={"entity_id": entity_id})
                current_humidity = _safe_float(attrs.get("humidity"))
                if current_humidity is None or abs(current_humidity - self.target_humidity) >= HUMIDITY_SEND_TOLERANCE_PCT:
                    self.ws.call_service("humidifier", "set_humidity", service_data={"humidity": self.target_humidity}, target={"entity_id": entity_id})
            elif state is not None and state.get("state") != "off":
                self.ws.call_service("humidifier", "turn_off", target={"entity_id": entity_id})

    # ---------------------------------------------------------- comandos --

    def set_temperature(self, single: float | None = None, low: float | None = None, high: float | None = None) -> None:
        if single is None and low is None and high is None:
            return
        if self._preset_mode != presets_module.PRESET_MANUAL:
            self._manual_heat = self.target_temperature_low if self.hvac_mode == "heat_cool" \
                else (self.target_temperature if self.hvac_mode == "heat" else None)
            self._manual_cool = self.target_temperature_high if self.hvac_mode == "heat_cool" \
                else (self.target_temperature if self.hvac_mode == "cool" else None)

        if self.hvac_mode == "heat_cool":
            if low is not None:
                self._manual_heat = float(low)
            if high is not None:
                self._manual_cool = float(high)
        elif self.hvac_mode == "heat" and single is not None:
            self._manual_heat = float(single)
        elif self.hvac_mode == "cool" and single is not None:
            self._manual_cool = float(single)
        else:
            return

        self._preset_mode = presets_module.PRESET_MANUAL
        self.decide_and_act()

    def set_humidity(self, humidity: float) -> None:
        self.target_humidity = float(humidity)
        self.decide_and_act()

    def set_hvac_mode(self, hvac_mode: str) -> None:
        self.hvac_mode = hvac_mode
        if hvac_mode != "off":
            self._last_active_hvac_mode = hvac_mode
        self.decide_and_act()

    def turn_off(self) -> None:
        self.set_hvac_mode("off")

    def turn_on(self) -> None:
        target = self._last_active_hvac_mode
        if target is None or target not in (self.hvac_modes or []):
            target = self._default_hvac_mode(self._last_full_capability)
        self.set_hvac_mode(target)

    def set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode not in (self._preset_modes or []):
            return
        if preset_mode == presets_module.PRESET_MANUAL and self._preset_mode != presets_module.PRESET_MANUAL:
            self._manual_heat = self.target_temperature_low if self.hvac_mode == "heat_cool" \
                else (self.target_temperature if self.hvac_mode == "heat" else None)
            self._manual_cool = self.target_temperature_high if self.hvac_mode == "heat_cool" \
                else (self.target_temperature if self.hvac_mode == "cool" else None)
        self._preset_mode = preset_mode
        self.decide_and_act()

    def set_fan_mode(self, fan_mode: str) -> None:
        self._manual_fan_mode = None if fan_mode == "auto" else fan_mode
        self._fan_mode = fan_mode
        self.decide_and_act()
