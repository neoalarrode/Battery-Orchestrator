"""
Constantes necesarias para el descubrimiento LAN y la nube de Tuya --
subconjunto minimo del `const.py` original (el resto de ese fichero es
especifico de ConfigEntry/config_flow de Home Assistant, no aplica aqui).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Descubrimiento LAN por broadcast UDP (ver discovery.py) -- mismos valores
# que el proyecto original, verificados contra tinytuya/localtuya.
# ---------------------------------------------------------------------------
UDP_PORT_UNENCRYPTED = 6666
UDP_PORT_ENCRYPTED = 6667
UDP_PORT_APP = 7000
UDP_KEY_SEED = b"yGAdlopoPVldABfn"  # MD5 de esto es la clave AES real, ver discovery.py
DISCOVERY_TIMEOUT = 8

# ---------------------------------------------------------------------------
# Tuya Cloud API regions (endpoints oficiales, documentados en
# https://developer.tuya.com/en/docs/iot/api-request) -- solo se usa al
# vincular una cuenta para traer local_keys/esquemas, nunca en operacion
# normal (100% LAN despues de eso).
# ---------------------------------------------------------------------------
TUYA_REGIONS = {
    "eu": "https://openapi.tuyaeu.com",
    "us": "https://openapi.tuyaus.com",
    "cn": "https://openapi.tuyacn.com",
    "in": "https://openapi.tuyain.com",
}
