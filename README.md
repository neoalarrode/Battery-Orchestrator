<p align="center">
  <img src="logo.png" width="120" alt="Home Orchestrator">
</p>

<h1 align="center">Home Orchestrator</h1>

<p align="center">
  Plataforma de automatización doméstica para Home Assistant — baterías, clima,<br>
  iluminación y más, cada una con su propio motor determinista. Sin cajas negras.
</p>

<p align="center">
  <img alt="Home Assistant Add-on" src="https://img.shields.io/badge/Home%20Assistant-Add--on-8b5cf6?style=flat-square&labelColor=0b0a16">
  <img alt="Determinista" src="https://img.shields.io/badge/planificador-determinista-22d3ee?style=flat-square&labelColor=0b0a16">
  <img alt="Sin cajas negras" src="https://img.shields.io/badge/sin%20cajas%20negras-eae8f7?style=flat-square&labelColor=0b0a16">
</p>

<p align="center">
  🇪🇸 Español · <a href="README.en.md">🇬🇧 Read in English</a>
</p>

---

<p align="center">
  <img src="screenshots/estado-actual.png" alt="Pestaña Estado actual del plugin Energy: SOC agregado, tramo tarifario, ahorro acumulado y cuenta atrás a la próxima punta" width="100%">
</p>
<p align="center"><em>Datos de ejemplo — no son de una instalación real.</em></p>

Un solo add-on de Home Assistant, varios plugins independientes que
instalas solo si los necesitas — cada uno con su propia lógica de
decisión legible de arriba a abajo, nunca un solver opaco ni una caja
negra. Empezó como un planificador de baterías (Energy); hoy cubre
también climatización, iluminación adaptativa, la integración de
dispositivos Tuya/TP-Link sin depender de la nube, y monitorización de
Starlink.

## Plugins

| Plugin | Qué hace |
|---|---|
| ⚡ **Energy** | Carga/descarga adaptativa de baterías domésticas por precio de la luz, producción solar y consumo real, más cargas diferibles (lavadora, lavavajillas...) — ver detalle más abajo. |
| 🌡️ **Climate** | Termostatos adaptativos por zona, expuestos como `climate.*` nativos de HA (HomeKit/Matter incluido) — presets por presencia, previsión de 24h, tarjeta de termostato interactiva. |
| 💡 **Lighting** | Iluminación adaptativa: color y brillo siguen la posición real del sol (nunca una hora fija), encendido/apagado por presencia, reglas condicionales por zona, control manual desde el propio dashboard. |
| 🛰️ **Starlink** | Monitorización de tu Starlink (rendimiento, latencia, obstrucción del cielo, alineación, consumo) — integra el proyecto open-source [Dishylink](https://github.com/DaveyHert/dishylink) tal cual, con un proxy local al dish. |
| 🔗 **Tuya** | Puente de ingesta para dispositivos Tuya por LAN — consumo interno por Climate/Lighting y/o exposición opcional a HA por MQTT, sin pasar por la nube de Tuya. |
| 🔗 **TP-Link** | Igual que Tuya pero para Kasa/Tapo, vía `python-kasa` (la misma librería que usa el propio componente TP-Link de Home Assistant). |

Instala solo lo que uses — cada plugin se descarga bajo demanda desde la
propia interfaz, verificado por checksum contra este mismo repositorio.
Energy es el único con un dashboard pensado para ser la app principal
del add-on; sin él instalado, la raíz muestra un catálogo mínimo para
elegir qué instalar.

## Energy, en detalle

Add-on de Home Assistant que planifica y ejecuta la carga/descarga de tus
baterías domésticas cada minuto, en directo contra tu instalación real.
Nada de Node-RED, nada de EMHASS: un motor propio, determinista y legible
de arriba a abajo, más una interfaz web donde declaras tú mismo cada
batería, precio y sensor — nada viene precargado ni oculto.

## Por qué existe

Las soluciones habituales (EMHASS, programación lineal genérica) resuelven
bien el problema pero esconden la lógica detrás de parámetros que cuesta
razonar y de un solver que no explica sus decisiones. Energy hace lo
contrario: un algoritmo de dos pasadas que puedes leer entero,
donde cada decisión de cada hora viene con su motivo en texto plano
("cargando en valle para cubrir la punta siguiente", "bloqueada: llena y
con excedente solar"...).

## Qué hace

- **Planifica** hora a hora combinando tarifa (fija punta/llano/valle o
  PVPC dinámico), previsión solar (sensor de HA o API de Forecast.Solar) y
  consumo real reconstruido a partir del histórico de tu propia
  instalación — sin aprendizaje automático opaco.
- **Reparte la carga** entre todas tus baterías proporcional a su
  capacidad real, y deja que cada una se autogestione al descargar (con
  el límite de potencia correcto en cada caso: máximo salvo que esté
  llena y sobre sol, entonces 0W para no autodrenarse).
- **Respeta tus límites**: SOC máximo/mínimo por batería, potencia
  contratada, reserva de energía para la punta futura incluso si hace
  falta cargar en llano de emergencia.
- **Estima la salud real de cada batería** observando cuánta energía hace
  falta para mover su SOC un tramo grande, y comparándolo con la
  capacidad que declaraste — no un contador de ciclos a ciegas.
- **Calcula el ahorro real acumulado**, comparando lo que has pagado con
  lo que habrías pagado sin batería, hora a hora.
- **Avisa de consumos anómalos**: si el consumo real se dispara muy por
  encima de lo esperado y se sostiene varios ciclos, lo marca en la
  interfaz y notifica en Home Assistant — con el detalle siempre a la
  vista, nunca solo un aviso sin explicación.
- **Prioridad configurable**: ahorro (por defecto), autoconsumo solar
  puro (nunca carga desde red) o longevidad de batería (no supera el 90%
  de SOC).
- **Cargas diferibles**: lavadora, lavavajillas, termo eléctrico... cualquier
  electrodoméstico con un enchufe/switch controlable. Tú eliges la
  frecuencia (puntual, diaria o varias veces al día, con días de la semana
  concretos si quieres) y si se puede interrumpir a medias o no; la app
  decide sola la hora que más conviene, con excedente solar o, en su
  defecto, la más barata — sin disparar falsas alarmas de consumo anómalo.
- **Estado en vivo**: SOC, solar y consumo se refrescan cada 5 segundos
  leyendo directo de Home Assistant, sin esperar al próximo ciclo completo
  de optimización.
- **Panel de solo lectura (wallpanel)**: además de Ingress, un puerto
  propio para dejar el panel fijo en una tablet de pared (WallPanel, Fully
  Kiosk...) sin pasar por el login de Home Assistant — sin acceso alguno a
  la configuración, bloqueado también en el servidor, no solo oculto en la
  interfaz.
- **Todo configurable desde la web**: baterías, tarifa, paneles solares,
  sensor de consumo — nada hardcodeado salvo la URL base de la API
  gratuita de Forecast.Solar. Configuración exportable/importable en un
  archivo, por si reinstalas el add-on.

## Capturas

<table>
<tr>
<td width="50%"><img src="screenshots/prevision.png" alt="Gráfica de SOC previsto a lo largo del día"></td>
<td width="50%"><img src="screenshots/salud-bateria.png" alt="Pestaña Salud de batería: capacidad real estimada vs. declarada"></td>
</tr>
</table>

Más capturas (configuración, alerta de consumo anómalo) en la [wiki](https://github.com/neoalarrode/Home-Orchestrator/wiki/Energy).

## Instalación

1. En Home Assistant: **Ajustes → Add-ons → Tienda de add-ons → ⋮ →
   Repositorios**, y añade:
   ```
   https://github.com/neoalarrode/Home-Orchestrator
   ```
2. Busca "Home Orchestrator" en la tienda, instálalo e inícialo.
3. Ábrelo desde el panel lateral (usa Ingress, no expone ningún puerto) —
   al arrancar sin nada instalado todavía verás un catálogo mínimo para
   elegir qué plugin(s) dar de alta.

Instrucciones de configuración paso a paso, plugin por plugin, en la
[wiki](https://github.com/neoalarrode/Home-Orchestrator/wiki).

## Estado del proyecto

En uso activo y en desarrollo — ver [CHANGELOG.md](CHANGELOG.md).
Empieza siempre en modo simulación: verás exactamente lo que haría el
add-on sin tocar tus baterías de verdad, hasta que confíes en sus
decisiones.

## Licencia

© 2026 Eric Larrodé. Todos los derechos reservados — ver [LICENSE](LICENSE).
El código es visible para poder instalarlo como add-on, pero no está
autorizado su uso, copia ni modificación fuera de este repositorio sin
permiso expreso.

El plugin Starlink vendoriza el build web oficial de
[Dishylink](https://github.com/DaveyHert/dishylink) (© daveyhert,
licencia MIT), con un único cambio de código fuente antes de compilar
(necesario para que funcione fuera de la raíz del dominio — ver
`app/starlink_dist/PATCH.md`) — licencia en
`app/starlink_dist/DISHYLINK_LICENSE.txt`.
