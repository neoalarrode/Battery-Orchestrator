"""
Reglas condicionales por zona: primera que coincide gana -- el mismo
patron "primera coincidencia" que ya usa Climate para resolver presets
(ver climate/presets.py), aplicado aqui a "que grupo de luces enciendo".
Una regla sin condiciones (`conditions: []`) siempre coincide -- se usa
como reserva/por defecto, normalmente la ultima de la lista (ver ejemplo
de la interfaz: "si el TV del salon esta encendido, enciende las de los
laterales; si no, la del techo" son dos reglas, la segunda sin condicion).

Cada condicion es `{"entity_id": str, "state": str | [str] | None}`:
  - `state` ausente/None -> solo exige que la entidad exista y no este en
    "unavailable"/"unknown" (comprobacion de "existe algo", no de un
    valor concreto).
  - `state` como string o lista -> el estado actual tiene que ser
    exactamente ese valor, o estar en esa lista (permite "media_player en
    playing O paused", por ejemplo).
"""

from __future__ import annotations


def _split_target(target: str) -> tuple[str, str | None]:
    """«media_player.tv» -> ("media_player.tv", None)
    «media_player.tv.media_content_type» -> ("media_player.tv", "media_content_type")

    Un entity_id de HA es siempre `dominio.objeto` (UN punto), asi que todo lo
    que venga a partir del segundo punto es el nombre del atributo. Con dos
    trozos o menos no hay atributo: se compara el ESTADO, como siempre."""
    parts = target.split(".")
    if len(parts) <= 2:
        return target, None
    return ".".join(parts[:2]), ".".join(parts[2:])


def _condition_matches(cond: dict, states: dict[str, dict]) -> bool:
    entity_id = cond.get("entity_id")
    if not entity_id:
        return True
    st = states.get(entity_id) or {}
    attribute = cond.get("attribute")
    negate = bool(cond.get("negate"))

    if attribute:
        actual = (st.get("attributes") or {}).get(attribute)
        # Un atributo AUSENTE es "sin dato", igual que una entidad no
        # disponible: ver el porque justo debajo.
        no_data = actual is None
    else:
        actual = st.get("state")
        no_data = actual in (None, "unavailable", "unknown")

    wanted = cond.get("state")
    if wanted is None:
        # «si entidad» sin valor: la condicion es "existe y se puede leer".
        return no_data == negate

    # SIN DATO no coincide NUNCA, ni en afirmativo ni en negativo. En
    # afirmativo ya era asi de hecho (`None in ["playing"]` es False); lo que
    # se decide aqui es el caso NUEVO, el negativo: una entidad no disponible
    # NO cumple un «!=», porque dar por bueno algo que no se puede comprobar es
    # justo lo que haria que una regla se disparase sola cuando HA tiene un
    # hipo. Mismo criterio que el resto del proyecto con los datos ausentes
    # (nunca un cero/True inventado).
    if no_data:
        return False

    wanted_list = wanted if isinstance(wanted, list) else [wanted]
    actual_str = actual if isinstance(actual, str) else str(actual)
    match = actual_str in wanted_list
    if not match and attribute:
        # Los atributos, a diferencia de los estados, no son siempre texto en
        # minusculas (numeros, booleanos, nombres propios) -- se compara sin
        # distinguir mayusculas SOLO en esta rama, para no cambiar en nada el
        # comportamiento de siempre de los estados.
        match = actual_str.lower() in [str(w).lower() for w in wanted_list]
    return match != negate


def select_rule(rules: list[dict], states: dict[str, dict]) -> dict | None:
    """`states` = `{entity_id: {"state": ...}}`, ya resuelto de antemano
    (una sola lectura de HA por ciclo, ver ZoneRunner._snapshot_states) --
    devuelve la primera regla cuyas condiciones (TODAS, en AND) se
    cumplen, o `None` si ninguna regla coincide (zona sin reserva por
    defecto y ninguna condicion activa: no se enciende nada)."""
    for rule in rules or []:
        conditions = rule.get("conditions") or []
        if all(_condition_matches(c, states) for c in conditions):
            return rule
    return None


def all_lights(rules: list[dict]) -> set[str]:
    out: set[str] = set()
    for rule in rules or []:
        out |= {e for e in (rule.get("lights") or []) if e}
    return out


def _parse_light_entry(raw: str) -> tuple[str, bool, bool]:
    """«light.x», «light.x:solo_brillo» o «light.x:solo_encendido» ->
    (entity_id, solo_brillo, solo_encendido). El sufijo «:solo_brillo»
    excluye esa luz en concreto del cambio de color/temperatura de color
    de la curva solar de la zona -- sigue encendiendose/apagandose y
    ajustando BRILLO con normalidad, solo se le deja de mandar color.
    Pensado para una luz que no soporta color, o que el usuario prefiere
    dejar siempre en un tono fijo (p.ej. una lampara de lectura) dentro
    de una zona que por lo demas si varia de color -- sin tener que
    sacarla a su propia regla/zona aparte.

    «:solo_encendido» (a peticion expresa del usuario, para las lamparas
    del Salon) va un paso mas alla: la excluye TANTO del color como del
    brillo -- la curva solar de la zona solo la enciende/apaga, nunca le
    toca ningun valor. Pensado para una lampara que el usuario quiere
    ajustar siempre a mano (con su propio dimmer, o porque no tiene
    sentido variarla con el sol) pero que aun asi quiere que seleccione
    la propia zona segun la regla activa/presencia.

    BUG REAL, confirmado en produccion: la version anterior partia por
    el PRIMER «:» sin mas («if ":" in raw: entity_id, flag = raw.split
    (":", 1)») -- rompia CUALQUIER referencia de bridge («tplink:
    <device_id>», «tuya:<device_id>»), que YA usa ":" como separador
    propio. «tplink:76812943» se leia como luz "tplink" a secas (el id
    del dispositivo se interpretaba, y descartaba, como si fuera el
    flag «solo_brillo»). Roto desde que se añadio este sufijo (misma
    version) para toda zona con luces TP-Link/Tuya directas -- visto
    tal cual en produccion: las bombillas de Cocina se quedaban
    encendidas para siempre, `all_lights()` ni siquiera las reconocia
    como las luces reales que son. Fix: el sufijo SOLO cuenta si el
    texto entero TERMINA en el sufijo esperado -- una referencia de
    bridge nunca termina asi, asi que nunca coincide por error."""
    if raw.lower().endswith(":solo_encendido"):
        return raw[: -len(":solo_encendido")].strip(), False, True
    if raw.lower().endswith(":solo_brillo"):
        return raw[: -len(":solo_brillo")].strip(), True, False
    return raw.strip(), False, False


def parse_rules_text(text: str) -> list[dict]:
    """Convierte el texto declarado en el asistente en una lista de
    reglas -- mismo espiritu que `climate/presets.py:parse_presets`, pero
    UNA REGLA POR LINEA (no separadas por comas: una regla ya usa comas
    para su propia lista de luces y de valores posibles de una condicion,
    asi que una coma como separador de reglas seria ambiguo). Dentro de
    una linea, los campos SI van separados por «;»:

        Nombre; si entidad=valor[,valor2,...]; si otra_entidad=valor; luces=light.a,light.b

    - El primer trozo es siempre el nombre.
    - Cualquier trozo «si entidad=valor» añade una condicion (varias
      condiciones en la misma regla van en AND). Varios valores separados
      por comas significan "cualquiera de estos" (OR dentro de esa
      condicion).
    - «si entidad!=valor» es la NEGACION: coincide cuando el estado no es
      ninguno de los valores dados.
    - «si entidad.atributo=valor» (y su «!=») compara un ATRIBUTO en vez del
      estado. El entity_id es siempre `dominio.objeto`, asi que lo que venga a
      partir del segundo punto es el nombre del atributo -- p.ej.
      «si media_player.salon_tv.media_content_type!=music» para que la regla
      NO se aplique cuando lo que suena es musica.
    - Una entidad no disponible (o un atributo ausente) NO cumple ninguna
      condicion, ni afirmativa ni negativa: dar por bueno un «!=» que no se
      puede comprobar haria que la regla se disparase sola cada vez que HA
      tuviera un hipo.
    - El trozo «luces=...» (obligatorio, al menos una) es la lista de
      `light.*` que controla esta regla. Una luz puede llevar el sufijo
      «:solo_brillo» («light.x:solo_brillo») para excluirla del cambio de
      color/temperatura de color de la curva -- sigue encendiendose y
      ajustando brillo con normalidad, solo se le deja de mandar color
      (util para una luz sin color, o que se quiere dejar siempre en un
      tono fijo dentro de una zona que por lo demas si varia).
    - Una regla SIN ninguna condicion siempre coincide -- se usa como
      reserva/por defecto; el orden de las lineas es el orden de
      evaluacion (la primera regla cuyas condiciones se cumplen gana).

    Ejemplo: «TV encendida; si media_player.salon_tv=playing,paused;
    luces=light.lateral_izq,light.lateral_der» en la primera linea y
    «Normal; luces=light.techo_salon» en la segunda (sin condicion, hace
    de reserva). Lanza ValueError con un mensaje legible si el texto no
    tiene el formato esperado."""
    parsed: list[dict] = []
    seen_names = set()
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        chunks = [c.strip() for c in line.split(";") if c.strip()]
        if not chunks:
            continue
        name = chunks[0]
        if not name:
            raise ValueError(f"«{line}» no declara un nombre de regla")
        if name in seen_names:
            raise ValueError(f"la regla «{name}» esta repetida")
        seen_names.add(name)

        conditions: list[dict] = []
        lights: list[str] = []
        brightness_only: list[str] = []
        on_off_only: list[str] = []
        for chunk in chunks[1:]:
            low = chunk.lower()
            if low.startswith("si "):
                cond_str = chunk[3:].strip()
                # «!=» se comprueba ANTES que «=», porque lo contiene.
                negate = "!=" in cond_str
                sep = "!=" if negate else "="
                if sep not in cond_str:
                    raise ValueError(
                        f"«{chunk}» no tiene el formato «si entidad=valor» (o «!=») en la regla «{name}»"
                    )
                target, values_str = cond_str.split(sep, 1)
                target = target.strip()
                values = [v.strip() for v in values_str.split(",") if v.strip()]
                if not target or not values:
                    raise ValueError(f"«{chunk}» no es una condicion valida en la regla «{name}»")
                entity_id, attribute = _split_target(target)
                conditions.append({
                    "entity_id": entity_id,
                    "attribute": attribute,
                    "state": values[0] if len(values) == 1 else values,
                    "negate": negate,
                })
            elif low.startswith("luces="):
                entries = [e.strip() for e in chunk[len("luces="):].split(",") if e.strip()]
                lights = []
                brightness_only = []
                on_off_only = []
                for entry in entries:
                    entity_id, only_brightness, only_on_off = _parse_light_entry(entry)
                    if entity_id:
                        lights.append(entity_id)
                        if only_brightness:
                            brightness_only.append(entity_id)
                        elif only_on_off:
                            on_off_only.append(entity_id)
            else:
                raise ValueError(
                    f"«{chunk}» no se reconoce en la regla «{name}» (usa «si entidad=valor» o «luces=...»)"
                )
        if not lights:
            raise ValueError(f"la regla «{name}» no declara ninguna luz («luces=...»)")
        parsed.append({
            "name": name, "conditions": conditions, "lights": lights,
            "brightness_only": brightness_only, "on_off_only": on_off_only,
        })
    if not parsed:
        raise ValueError("declara al menos una regla (una sin «si...» sirve de reserva por defecto)")
    return parsed


def rules_to_text(rule_list: list[dict]) -> str:
    """Inverso de `parse_rules_text` -- para rellenar el textarea al
    editar una zona ya existente."""
    lines = []
    for r in rule_list or []:
        chunks = [r.get("name", "")]
        for c in r.get("conditions") or []:
            state = c.get("state")
            state_str = ",".join(state) if isinstance(state, list) else (state or "")
            target = c.get("entity_id", "")
            if c.get("attribute"):
                target = f"{target}.{c['attribute']}"
            chunks.append(f"si {target}{'!=' if c.get('negate') else '='}{state_str}")
        brightness_only = set(r.get("brightness_only") or [])
        on_off_only = set(r.get("on_off_only") or [])
        light_entries = []
        for e in (r.get("lights") or []):
            if e in brightness_only:
                light_entries.append(f"{e}:solo_brillo")
            elif e in on_off_only:
                light_entries.append(f"{e}:solo_encendido")
            else:
                light_entries.append(e)
        chunks.append("luces=" + ",".join(light_entries))
        lines.append("; ".join(chunks))
    return "\n".join(lines)


def condition_entities(rules: list[dict]) -> set[str]:
    out: set[str] = set()
    for rule in rules or []:
        for c in rule.get("conditions") or []:
            eid = c.get("entity_id")
            if eid:
                out.add(eid)
    return out
