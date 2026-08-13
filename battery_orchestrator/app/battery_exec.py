"""
Reparto de la decision agregada (cargar X W / descargar Y W) entre las N
baterias que el usuario haya declarado, proporcional a su capacidad real,
y ejecucion contra Home Assistant (o solo simulacion en modo dry-run).
"""

from __future__ import annotations

from dataclasses import dataclass

import ecoflow_ble
import ecoflow_cloud
import ha_client

# Campos del estado en vivo de EcoFlow Cloud que pueden traer el SOC, por
# orden de preferencia — distintos modelos/firmwares de la familia STREAM
# reportan uno u otro (ver hallazgos en ecoflow_cloud.py: el snapshot REST
# y el feed MQTT no siempre coinciden en que campo rellenan primero).
ECOFLOW_SOC_FIELDS = ("cmsBattSoc", "bmsBattSoc", "soc", "f32ShowSoc")


@dataclass
class Battery:
    id: str
    name: str
    capacity_wh: float
    soc_sensor: str = ""
    charge_switch: str = ""
    discharge_switch: str = ""
    max_charge_w: float = 1200
    max_discharge_w: float = 1200
    min_soc_pct: float = 3
    max_soc_pct: float = 100
    charge_power_limit_entity: str | None = None
    discharge_power_limit_entity: str | None = None
    # Fuente EcoFlow (ver ecoflow_cloud.py / ecoflow_ble.py) en vez de
    # entidades de HA declaradas a mano — "source" decide que bloque de
    # campos de arriba/abajo se usa de verdad para esta bateria en
    # concreto. Cada bateria del sistema puede tener una fuente distinta,
    # se deciden una a una. "ecoflow_mode" es generico a proposito
    # (bluetooth/cloud/hybrid) para que una marca futura que no sea
    # EcoFlow pueda reutilizar el mismo patron de despacho sin rediseñar
    # nada de aqui — solo necesitaria su propio modulo tipo `ecoflow_ble`/
    # `ecoflow_cloud` con las mismas 4 funciones (get_state/discover/
    # set_charging_task/set_discharging_task).
    source: str = "ha"  # "ha" | "ecoflow"
    ecoflow_mode: str | None = None  # "bluetooth" | "cloud" | "hybrid" (solo si source == "ecoflow")
    ecoflow_sn: str | None = None            # modo cloud/hybrid: sn de ESTA unidad dentro del grupo
    ecoflow_main_sn: str | None = None       # modo cloud/hybrid: sn del dispositivo "principal" del grupo
    ecoflow_ble_address: str | None = None   # modo bluetooth/hybrid: direccion BLE de ESTA unidad
    ecoflow_access_key: str | None = None    # credenciales de cuenta EcoFlow (globales), solo si hace falta cloud
    ecoflow_secret_key: str | None = None
    ecoflow_user_id: str | None = None       # userId de cuenta EcoFlow (global), solo si hace falta bluetooth
    # De donde vino el ULTIMO SOC leido con exito ("bluetooth" | "cloud"),
    # solo informativo -- para el iconito BT/API del dashboard. Se
    # actualiza cada vez que se llama a read_soc_pct() en una bateria
    # EcoFlow; None si no es EcoFlow o si esta ultima lectura fallo.
    last_ecoflow_source: str | None = None

    def read_soc_pct(self) -> float | None:
        """None si el sensor esta 'unavailable'/'unknown' o no responde (o,
        en EcoFlow, si el feed en vivo/la conexion BLE todavia no ha dicho
        nada). No se inventa un 50% - el llamante debe saltarse esta
        bateria."""
        if self.source == "ecoflow":
            return self._read_ecoflow_soc_pct()
        return ha_client.get_numeric_state(self.soc_sensor, default=None)

    def _read_ecoflow_soc_pct_via_ble(self) -> float | None:
        if not (self.ecoflow_ble_address and self.ecoflow_user_id):
            return None
        state = ecoflow_ble.get_state(self.ecoflow_ble_address, self.ecoflow_user_id)
        # "battery_level" es el SOC agregado del grupo BKW (todas las
        # unidades enlazadas); "battery_level_main" es el de ESTA unidad,
        # que es lo que coincide con lo que muestra la app oficial de
        # EcoFlow y con lo que Battery Orchestrator espera por bateria.
        val = state.get("battery_level_main") if state else None
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def _read_ecoflow_soc_pct_via_cloud(self) -> float | None:
        if not (self.ecoflow_sn and self.ecoflow_access_key and self.ecoflow_secret_key):
            return None
        client = ecoflow_cloud.get_client(self.ecoflow_access_key, self.ecoflow_secret_key)
        if client is None:
            return None
        state = client.get_live_state(self.ecoflow_sn, required_fields=ECOFLOW_SOC_FIELDS)
        if not state:
            return None
        for field in ECOFLOW_SOC_FIELDS:
            val = state.get(field)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return None

    def _read_ecoflow_soc_pct(self) -> float | None:
        self.last_ecoflow_source = None
        if self.ecoflow_mode == "bluetooth":
            val = self._read_ecoflow_soc_pct_via_ble()
            self.last_ecoflow_source = "bluetooth" if val is not None else None
            return val
        if self.ecoflow_mode == "hybrid":
            val = self._read_ecoflow_soc_pct_via_ble()
            if val is not None:
                self.last_ecoflow_source = "bluetooth"
                return val
            val = self._read_ecoflow_soc_pct_via_cloud()
            self.last_ecoflow_source = "cloud" if val is not None else None
            return val
        val = self._read_ecoflow_soc_pct_via_cloud()  # "cloud" (o valor no reconocido: mejor caer aqui que no leer nada)
        self.last_ecoflow_source = "cloud" if val is not None else None
        return val

    def read_live_power_w(self) -> float | None:
        """
        Potencia neta en vivo de esta bateria EcoFlow — positivo cargando,
        negativo descargando, `None` si no hay dato de verdad. SOLO por
        BLE se puede leer por UNIDAD (Cloud reporta un agregado de todo el
        grupo, ver `_live_battery_charge_discharge_w` en main.py, que
        gestiona ese caso aparte para no duplicarlo si hay varias baterias
        del mismo grupo declaradas). No aplica a baterias "ha" — esas ya
        se leen en main.py con su propia logica de power_sensor_mode.
        """
        if self.source != "ecoflow" or self.ecoflow_mode not in ("bluetooth", "hybrid"):
            return None
        if not (self.ecoflow_ble_address and self.ecoflow_user_id):
            return None
        state = ecoflow_ble.get_state(self.ecoflow_ble_address, self.ecoflow_user_id)
        val = state.get("battery_power") if state else None
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    def ecoflow_set_charging_task(
        self, enable: bool | None = None, power_limit_w: float | None = None, target_soc: float | None = None,
    ) -> bool:
        """Activa/desactiva la tarea de carga y/o ajusta su limite de
        potencia y SOC objetivo, por el camino que toque segun el modo de
        esta bateria. Devuelve False (nunca lanza) si el modo elegido no
        tiene los datos que necesita — quien llama decide que hacer con
        eso (`execute()` lo convierte en un aviso, no en un fallo duro)."""
        def via_ble() -> bool:
            if not (self.ecoflow_ble_address and self.ecoflow_user_id):
                return False
            return ecoflow_ble.set_charging_task(
                self.ecoflow_ble_address, self.ecoflow_user_id,
                enable=enable, power_limit_w=power_limit_w, target_soc=target_soc,
            )

        def via_cloud() -> bool:
            if not (self.ecoflow_access_key and self.ecoflow_secret_key and self.ecoflow_sn and self.ecoflow_main_sn):
                return False
            client = ecoflow_cloud.get_client(self.ecoflow_access_key, self.ecoflow_secret_key)
            if client is None:
                return False
            return client.set_charging_task(
                self.ecoflow_main_sn, self.ecoflow_sn,
                enable=enable, power_limit_w=power_limit_w, target_soc=target_soc,
            )

        if self.ecoflow_mode == "bluetooth":
            return via_ble()
        if self.ecoflow_mode == "hybrid":
            # BLE primero (limites en vatios de verdad, mas preciso) — Cloud
            # solo como red de seguridad si BLE no responde (p.ej. fuera de
            # alcance del proxy), nunca al reves.
            return via_ble() or via_cloud()
        return via_cloud()

    def ecoflow_set_discharging_task(self, enable: bool | None = None, power_limit_w: float | None = None) -> bool:
        def via_ble() -> bool:
            if not (self.ecoflow_ble_address and self.ecoflow_user_id):
                return False
            return ecoflow_ble.set_discharging_task(
                self.ecoflow_ble_address, self.ecoflow_user_id, enable=enable, power_limit_w=power_limit_w,
            )

        def via_cloud() -> bool:
            if not (self.ecoflow_access_key and self.ecoflow_secret_key and self.ecoflow_main_sn):
                return False
            client = ecoflow_cloud.get_client(self.ecoflow_access_key, self.ecoflow_secret_key)
            if client is None:
                return False
            return client.set_discharging_task(self.ecoflow_main_sn, enable=enable, power_limit_w=power_limit_w)

        if self.ecoflow_mode == "bluetooth":
            return via_ble()
        if self.ecoflow_mode == "hybrid":
            return via_ble() or via_cloud()
        return via_cloud()


def _distribute(total_w: float, items: list[tuple[Battery, float, float]]) -> dict[str, float]:
    """
    Reparte `total_w` entre baterias proporcionalmente a `headroom` (3er
    elemento de cada tupla), sin superar el limite de potencia de cada una
    (2o elemento). Lo que una bateria no pueda absorber se reparte entre
    las demas.
    """
    result = {b.id: 0.0 for b, _, _ in items}
    remaining = total_w
    pending = [(b, cap_w, headroom) for b, cap_w, headroom in items if headroom > 0 and cap_w > 0]

    for _ in range(len(pending) + 1):
        if remaining <= 0 or not pending:
            break
        total_headroom = sum(h for _, _, h in pending)
        if total_headroom <= 0:
            break
        next_pending = []
        assigned_this_round = 0.0
        for b, cap_w, headroom in pending:
            share = remaining * (headroom / total_headroom)
            take = min(share, cap_w, headroom)
            result[b.id] += take
            assigned_this_round += take
            leftover_cap = cap_w - take
            leftover_headroom = headroom - take
            if leftover_cap > 1e-6 and leftover_headroom > 1e-6:
                next_pending.append((b, leftover_cap, leftover_headroom))
        remaining -= assigned_this_round
        pending = next_pending
        if assigned_this_round <= 1e-6:
            break

    return result


def plan_distribution(batteries: list[Battery], charge_w: float, discharge_w: float,
                       pv_surplus_w: float = 0.0) -> dict:
    """
    Lee el SOC real de cada bateria (una lectura por ciclo, todas a la vez).

    - Baterias cuyo sensor de SOC este 'unavailable'/'unknown': se excluyen
      de este ciclo por completo (ni cargan ni descargan), para no asumir
      un valor inventado.
    - Carga: se reparte proporcionalmente a la capacidad/hueco de cada
      bateria disponible.
    - Descarga: NO se reparte potencia entre baterias (cada una se
      autogestiona), pero el LIMITE de potencia de descarga si se fija
      siempre al maximo declarado por el usuario para esa bateria, salvo
      el caso de bloqueo: bateria ya al 100% (soc >= max_soc_pct) Y sigue
      habiendo excedente solar en ese momento -> el limite se pone a 0W
      para no dejarla autodescargarse sin necesidad mientras el sol ya
      cubre el consumo.
    """
    socs = {b.id: b.read_soc_pct() for b in batteries}
    unavailable = [b for b in batteries if socs[b.id] is None]
    available = [b for b in batteries if socs[b.id] is not None]

    # capacity_wh viaja en cada entrada para que quien consuma esto (la
    # interfaz) pueda comparar el SOC de cada bateria contra la media
    # ponderada por capacidad, no una media simple — con baterias de
    # tamaños muy distintos, una media simple sesga la comparacion hacia
    # las pequeñas.
    per_battery: list[dict] = [
        {"id": b.id, "name": b.name, "soc_pct": None, "power_w": 0, "capacity_wh": b.capacity_wh,
         "enabled": False, "note": "sensor de SOC no disponible, se omite este ciclo",
         "ecoflow_source": b.last_ecoflow_source if b.source == "ecoflow" else None}
        for b in unavailable
    ]

    if charge_w > 0 and available:
        items = []
        for b in available:
            soc_wh = socs[b.id] / 100 * b.capacity_wh
            max_soc_wh = b.max_soc_pct / 100 * b.capacity_wh
            headroom = max(0.0, max_soc_wh - soc_wh)
            items.append((b, b.max_charge_w, headroom))
        assigned = _distribute(charge_w, items)
        action = "charge"
        per_battery += [
            {"id": b.id, "name": b.name, "soc_pct": socs[b.id], "power_w": round(assigned[b.id]),
             "capacity_wh": b.capacity_wh, "enabled": assigned[b.id] > 1, "note": "reparto por capacidad",
             "ecoflow_source": b.last_ecoflow_source if b.source == "ecoflow" else None}
            for b in available
        ]
    elif discharge_w > 0 and available:
        action = "discharge"
        for b in available:
            soc_wh = socs[b.id] / 100 * b.capacity_wh
            min_soc_wh = b.min_soc_pct / 100 * b.capacity_wh
            has_margin = (soc_wh - min_soc_wh) > 0
            is_full = socs[b.id] >= b.max_soc_pct
            blocked = is_full and pv_surplus_w > 0
            if blocked:
                power_w, enabled, note = 0, False, "bloqueada: llena y con excedente solar (evitar autodescarga)"
            elif has_margin:
                power_w, enabled, note = round(b.max_discharge_w), True, "limite al maximo declarado"
            else:
                power_w, enabled, note = 0, False, "sin margen (al minimo)"
            per_battery.append({"id": b.id, "name": b.name, "soc_pct": socs[b.id], "power_w": power_w,
                                 "capacity_wh": b.capacity_wh, "enabled": enabled, "note": note,
                                 "ecoflow_source": b.last_ecoflow_source if b.source == "ecoflow" else None})
    else:
        action = "idle"
        per_battery += [
            {"id": b.id, "name": b.name, "soc_pct": socs[b.id], "power_w": 0, "capacity_wh": b.capacity_wh,
             "enabled": False, "note": "sin accion",
             "ecoflow_source": b.last_ecoflow_source if b.source == "ecoflow" else None}
            for b in available
        ]

    return {"action": action, "per_battery": per_battery}


def execute(batteries: list[Battery], distribution: dict, dry_run: bool = True) -> list[str]:
    """
    Aplica la distribucion a HA. En dry_run solo devuelve lo que HARIA.

    Cada bateria se manda por separado, envuelta en su propio try/except:
    un timeout o fallo puntual hablando con HA para UNA bateria no debe
    impedir que se les mande la orden al resto, ni tumbar el ciclo entero
    (la proxima pasada, 60s despues, ya lo reintenta solo). El aviso queda
    en el log de esa bateria en vez de desaparecer en una excepcion.
    """
    log_lines = []
    action = distribution["action"]
    by_id = {b.id: b for b in batteries}

    for entry in distribution["per_battery"]:
        b = by_id[entry["id"]]
        power = entry["power_w"]
        soc_txt = f"{entry['soc_pct']:.1f}%" if entry["soc_pct"] is not None else "N/D"

        if entry["soc_pct"] is None:
            line = f"[{b.name}] OMITIDA — {entry['note']}"
            log_lines.append(("[SIMULACION] " if dry_run else "") + line)
            continue

        # Semantica confirmada por el usuario para estos equipos (p.ej.
        # EcoFlow): cargar = switch de carga ON, switch de descarga OFF (a
        # secas); descargar = al reves. Pero "bloqueada"/"sin accion" NO es
        # "descarga OFF" sin mas: el switch de descarga se deja ACTIVO y es
        # el LIMITE de potencia a 0 el que de verdad corta la salida — en
        # estos modelos el switch de "tarea de descarga" es solo eso, una
        # tarea, no el interruptor fisico; con el limite a 0 sin el switch
        # activo puede no aplicarse, y con el switch apagado sin más el
        # equipo puede seguir descargando igual (como un SAI) para sostener
        # la carga conectada. Confirmado en real: bateria en "sin accion"
        # seguia descargando con el switch simplemente apagado.
        #
        # Para baterias EcoFlow (source == "ecoflow") es EXACTAMENTE la
        # misma logica de 4 casos, pero "switch"="tarea programada"
        # (isEnable de la tarea de carga/descarga) y "limite" =
        # chgFromGridPowerLimited / homeNeedPowerLimited, mandados por
        # BLE, Cloud o ambos segun `ecoflow_mode` (ver
        # `Battery.ecoflow_set_charging_task`/`ecoflow_set_discharging_task`)
        # — nunca se mezclan entidades de HA con comandos EcoFlow para la
        # misma bateria.
        is_ecoflow = b.source == "ecoflow"

        if action == "charge" and entry["enabled"]:
            line = f"[{b.name}] CARGAR a {power:.0f} W ({entry['note']}, SOC {soc_txt})"
            if is_ecoflow:
                def apply(b=b, power=power):
                    b.ecoflow_set_discharging_task(enable=False)
                    if not b.ecoflow_set_charging_task(enable=True, power_limit_w=power):
                        raise RuntimeError("EcoFlow no confirmo el comando de carga")
            else:
                def apply(b=b, power=power):
                    ha_client.turn_off(b.discharge_switch)
                    ha_client.turn_on(b.charge_switch)
                    if b.charge_power_limit_entity:
                        ha_client.set_number(b.charge_power_limit_entity, power)
        elif action == "discharge" and entry["enabled"]:
            line = f"[{b.name}] DESCARGA activada, limite {power:.0f} W ({entry['note']}, SOC {soc_txt})"
            if is_ecoflow:
                def apply(b=b, power=power):
                    b.ecoflow_set_charging_task(enable=False)
                    if not b.ecoflow_set_discharging_task(enable=True, power_limit_w=power):
                        raise RuntimeError("EcoFlow no confirmo el comando de descarga")
            else:
                def apply(b=b, power=power):
                    ha_client.turn_off(b.charge_switch)
                    ha_client.turn_on(b.discharge_switch)
                    if b.discharge_power_limit_entity:
                        ha_client.set_number(b.discharge_power_limit_entity, power)
        elif action == "discharge" and not entry["enabled"]:
            line = f"[{b.name}] descarga BLOQUEADA a 0W ({entry['note']}, SOC {soc_txt})"
            if is_ecoflow:
                def apply(b=b):
                    if not b.ecoflow_set_discharging_task(enable=True, power_limit_w=0):
                        raise RuntimeError("EcoFlow no confirmo el bloqueo de descarga")
            else:
                def apply(b=b):
                    if b.discharge_power_limit_entity:
                        ha_client.turn_on(b.discharge_switch)
                        ha_client.set_number(b.discharge_power_limit_entity, 0)
                    else:
                        ha_client.turn_off(b.discharge_switch)
        else:
            line = f"[{b.name}] sin accion (SOC {soc_txt})"
            if is_ecoflow:
                def apply(b=b):
                    b.ecoflow_set_charging_task(enable=False)
                    b.ecoflow_set_discharging_task(enable=True, power_limit_w=0)
            else:
                def apply(b=b):
                    ha_client.turn_off(b.charge_switch)
                    if b.discharge_power_limit_entity:
                        ha_client.turn_on(b.discharge_switch)
                        ha_client.set_number(b.discharge_power_limit_entity, 0)
                    else:
                        ha_client.turn_off(b.discharge_switch)

        if not dry_run:
            try:
                apply()
            except Exception as e:
                line += f" — AVISO: no se pudo aplicar en Home Assistant ({e})"

        log_lines.append(("[SIMULACION] " if dry_run else "") + line)

    return log_lines
