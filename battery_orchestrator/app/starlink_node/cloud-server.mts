// Servidor de fondo REAL para la cuenta de Starlink (nube) -- fichero NUEVO
// de esta integracion (no existe en el proyecto original), pero solo une
// piezas: usa `createCloudHandler` de `cloud/starlinkCloudHandler.ts`
// (codigo REAL del proyecto, sin tocar) exactamente igual que hace su
// propio plugin de desarrollo (`dev/starlinkCloudProxy.ts`, un plugin de
// Vite -- aqui no hay Vite en produccion, asi que esto es la misma logica
// como un `node:http` normal y corriente, standalone).
//
// Por que hace falta un servidor de verdad (y no se puede hacer desde el
// navegador): "the browser cannot call starlink.com directly (CORS: ACAO
// is starlink.com-only; the session cookies are HttpOnly/SameSite so JS
// can't attach them)" -- comentario real de `dev/starlinkCloudProxy.ts`.
// En los productos oficiales esto vive en el proceso principal de
// Electron o en el service worker de la extension; aqui vive en este
// proceso Node, proxied por el backend Python (`starlink_plugin.py`)
// exactamente igual que `/dishy` y `/router`.
//
// Configuracion por variables de entorno (mismo criterio que
// `collector/historian.mts`, nunca hardcodeado):
//   CLOUD_PORT           puerto de escucha (por defecto 8089)
//   CLOUD_COOKIE_FILE    donde persistir la sesion (en /data, para que
//                        sobreviva a reinicios del add-on)
//   DISH_URL             endpoint grpc-web del dish (por defecto el real)
//   ROUTER_URL           endpoint grpc-web del router -- CONFIGURABLE,
//                        a diferencia del dish: ver starlink_store.py,
//                        la IP por defecto (192.168.1.1) puede colisionar
//                        con el router propio de esta instalacion.
//   HISTORIAN_PROTOSET   ruta a dish.protoset (mismo fichero vendorizado
//                        que ya usa el front, ver public/dish.protoset)

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import { readFileSync, writeFileSync, rmSync } from "node:fs";
import { dirname } from "node:path";
import { mkdirSync } from "node:fs";
import { createCloudHandler } from "./cloud/starlinkCloudHandler.ts";
import { DishClient } from "./core/dishClient.ts";
import type { DishConfigJson } from "./core/dishClient.ts";
import { prepareDishConfigUpdate } from "./core/dishConfigUpdate.ts";
import { prepareRouterClientUpdate } from "./core/routerClientUpdate.ts";
import type { RouterClientUpdate } from "./core/routerClientUpdate.ts";
import { localNetworkIdentity } from "./core/hostNetworkIdentity.ts";

const PORT = Number(process.env.CLOUD_PORT ?? 8089);
const COOKIE_FILE = process.env.CLOUD_COOKIE_FILE ?? "/data/starlink/.starlink-cookie";
const DISH_URL =
  process.env.DISH_URL ?? "http://192.168.100.1:9201/SpaceX.API.Device.Device/Handle";
const ROUTER_URL =
  process.env.ROUTER_URL ?? "http://192.168.1.1:9001/SpaceX.API.Device.Device/Handle";
const PROTOSET_PATH = process.env.HISTORIAN_PROTOSET ?? "public/dish.protoset";

function readCookie(): string | null {
  try {
    return readFileSync(COOKIE_FILE, "utf8").trim();
  } catch {
    return null;
  }
}
function writeCookie(cookie: string): void {
  mkdirSync(dirname(COOKIE_FILE), { recursive: true });
  writeFileSync(COOKIE_FILE, cookie, "utf8");
}
function clearCookie(): void {
  try {
    rmSync(COOKIE_FILE);
  } catch {
    /* already gone */
  }
}

function sendJson(res: ServerResponse, status: number, payload: unknown) {
  res.statusCode = status;
  res.setHeader("content-type", "application/json");
  res.end(JSON.stringify(payload));
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolveBody, reject) => {
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => resolveBody(data));
    req.on("error", reject);
  });
}

let routerPromise: Promise<DishClient> | null = null;
let dishPromise: Promise<DishClient> | null = null;
const protosetBytes = () => new Uint8Array(readFileSync(PROTOSET_PATH));

const handler = createCloudHandler({
  readCookie,
  writeCookie,
  clearCookie,
  prepareDeviceUpdate: async (update) => {
    routerPromise ??= DishClient.load("router", {
      handleUrl: ROUTER_URL,
      protosetBytes: protosetBytes(),
    });
    return prepareRouterClientUpdate(await routerPromise, update, localNetworkIdentity());
  },
  prepareDishConfigUpdate: async (changes) => {
    dishPromise ??= DishClient.load("dish", {
      handleUrl: DISH_URL,
      protosetBytes: protosetBytes(),
    });
    return prepareDishConfigUpdate(await dishPromise, changes);
  },
});

// Sin `isLocalOrigin` (el plugin de desarrollo original la tiene porque su
// servidor escucha en todas las interfaces durante `npm run dev`) -- este
// servidor solo escucha en loopback (`127.0.0.1`, ver `.listen` mas abajo)
// y solo lo alcanza el propio backend Python de este add-on por el mismo
// host, nunca directamente un navegador.
createServer(async (req: IncomingMessage, res: ServerResponse) => {
  const url = req.url ?? "";
  if (!url.startsWith("/cloud/")) return sendJson(res, 404, { error: "not_found" });
  const route = url.split("?")[0];

  if (route === "/cloud/session") {
    if (req.method === "DELETE") {
      const { status, body } = handler.disconnect();
      return sendJson(res, status, body);
    }
    if (req.method === "POST") {
      try {
        const { cookie } = JSON.parse((await readBody(req)) || "{}") as { cookie?: string };
        const { status, body } = await handler.connect(cookie ?? "");
        return sendJson(res, status, body);
      } catch {
        return sendJson(res, 400, { error: "bad_request", message: "Expected JSON { cookie }." });
      }
    }
    return sendJson(res, 405, { error: "method_not_allowed" });
  }

  if (route === "/cloud/device" && req.method === "POST") {
    try {
      const update = JSON.parse((await readBody(req)) || "{}") as RouterClientUpdate;
      const result = await handler.updateClient(update);
      return sendJson(res, result.status, result.body);
    } catch (error) {
      return sendJson(res, 400, { error: "bad_request", message: (error as Error).message });
    }
  }

  if (route === "/cloud/dish-config" && req.method === "POST") {
    try {
      const changes = JSON.parse((await readBody(req)) || "{}") as DishConfigJson;
      const result = await handler.updateDishConfig(changes);
      return sendJson(res, result.status, result.body);
    } catch (error) {
      return sendJson(res, 400, { error: "bad_request", message: (error as Error).message });
    }
  }

  const { status, body } = await handler.handle(route);
  sendJson(res, status, body);
}).listen(PORT, "127.0.0.1", () => {
  console.log(`[cloud-server] listening on http://127.0.0.1:${PORT}`);
});
