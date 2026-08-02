<p align="center">
  <img src="logo.png" width="120" alt="Battery Orchestrator">
</p>

<h1 align="center">Battery Orchestrator</h1>

<p align="center">
  Carga y descarga adaptativa de baterías domésticas — por precio de la luz,<br>
  producción solar y consumo real. Sin cajas negras.
</p>

<p align="center">
  <img alt="Home Assistant Add-on" src="https://img.shields.io/badge/Home%20Assistant-Add--on-8b5cf6?style=flat-square&labelColor=0b0a16">
  <img alt="Determinista" src="https://img.shields.io/badge/planificador-determinista-22d3ee?style=flat-square&labelColor=0b0a16">
  <img alt="Sin cajas negras" src="https://img.shields.io/badge/sin%20cajas%20negras-eae8f7?style=flat-square&labelColor=0b0a16">
</p>

---

Add-on de Home Assistant que planifica y ejecuta la carga/descarga de tus
baterías domésticas cada minuto, en directo contra tu instalación real.
Nada de Node-RED, nada de EMHASS: un motor propio, determinista y legible
de arriba a abajo, más una interfaz web donde declaras tú mismo cada
batería, precio y sensor — nada viene precargado ni oculto.

## Por qué existe

Las soluciones habituales (EMHASS, programación lineal genérica) resuelven
bien el problema pero esconden la lógica detrás de parámetros que cuesta
razonar y de un solver que no explica sus decisiones. Battery Orchestrator
hace lo contrario: un algoritmo de dos pasadas que puedes leer entero,
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
- **Todo configurable desde la web**: baterías, tarifa, paneles solares,
  sensor de consumo — nada hardcodeado salvo la URL base de la API
  gratuita de Forecast.Solar. Configuración exportable/importable en un
  archivo, por si reinstalas el add-on.

## Instalación

1. En Home Assistant: **Ajustes → Add-ons → Tienda de add-ons → ⋮ →
   Repositorios**, y añade:
   ```
   https://github.com/neoalarrode/EF-HA-Orchestrator
   ```
2. Busca "Battery Orchestrator" en la tienda, instálalo e inícialo.
3. Ábrelo desde el panel lateral (usa Ingress, no expone ningún puerto).

Instrucciones de configuración paso a paso en [DOCS.md](DOCS.md).

## Estado del proyecto

En uso activo y en desarrollo — ver [CHANGELOG.md](CHANGELOG.md).
Empieza siempre en modo simulación: verás exactamente lo que haría el
add-on sin tocar tus baterías de verdad, hasta que confíes en sus
decisiones.
