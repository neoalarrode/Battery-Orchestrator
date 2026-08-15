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


def _condition_matches(cond: dict, states: dict[str, dict]) -> bool:
    entity_id = cond.get("entity_id")
    if not entity_id:
        return True
    actual = (states.get(entity_id) or {}).get("state")
    wanted = cond.get("state")
    if wanted is None:
        return actual not in (None, "unavailable", "unknown")
    wanted_list = wanted if isinstance(wanted, list) else [wanted]
    return actual in wanted_list


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
    - El trozo «luces=...» (obligatorio, al menos una) es la lista de
      `light.*` que controla esta regla.
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
        for chunk in chunks[1:]:
            low = chunk.lower()
            if low.startswith("si "):
                cond_str = chunk[3:].strip()
                if "=" not in cond_str:
                    raise ValueError(f"«{chunk}» no tiene el formato «si entidad=valor» en la regla «{name}»")
                entity_id, values_str = cond_str.split("=", 1)
                entity_id = entity_id.strip()
                values = [v.strip() for v in values_str.split(",") if v.strip()]
                if not entity_id or not values:
                    raise ValueError(f"«{chunk}» no es una condicion valida en la regla «{name}»")
                conditions.append({"entity_id": entity_id, "state": values[0] if len(values) == 1 else values})
            elif low.startswith("luces="):
                lights = [e.strip() for e in chunk[len("luces="):].split(",") if e.strip()]
            else:
                raise ValueError(
                    f"«{chunk}» no se reconoce en la regla «{name}» (usa «si entidad=valor» o «luces=...»)"
                )
        if not lights:
            raise ValueError(f"la regla «{name}» no declara ninguna luz («luces=...»)")
        parsed.append({"name": name, "conditions": conditions, "lights": lights})
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
            chunks.append(f"si {c.get('entity_id', '')}={state_str}")
        chunks.append("luces=" + ",".join(r.get("lights") or []))
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
