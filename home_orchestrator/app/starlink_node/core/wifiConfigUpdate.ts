import type { DishClient, WifiBasicServiceSetJson } from "./dishClient";

/** The subset of the router's writable WiFi/network fields this integration
 *  exposes. Field names and their `apply_*` flags are confirmed against the
 *  real `SpaceX.API.Device.WifiConfig` message (registry-introspected off the
 *  live `dish.protoset`, not guessed from `strings` output — an earlier pass
 *  of this file placed networkName/networkPassword directly on WifiConfig,
 *  which the real schema rejects: those three field names exist only on
 *  `WifiSetupRequest`, the first-run wizard message, not on the config this
 *  RPC edits). SSID/password live inside `networks[0].basicServiceSets[]`,
 *  same place the read side (`RouterSettingsTab.tsx`) already gets them from.
 *  Deliberately not exhaustive — see PATCH.md for the full field list. */
export interface WifiConfigChangesJson {
  /** Applied to every band's SSID unless `networkName5ghz` overrides the 5 GHz
   *  ones specifically — matches how this router currently broadcasts one
   *  name across 2.4/5/5 GHz-high (confirmed: all three bands share one SSID
   *  today). */
  networkName?: string;
  /** Only the 5 GHz and 5 GHz-high bands, leaving 2.4 GHz's name alone. */
  networkName5ghz?: string;
  /** Only rewrites bands whose current auth is WPA2 (this router's own — WPA3
   *  bands would need their own field, not exposed here). */
  networkPassword?: string;
  bypassMode?: boolean;
  customDnsDisabled?: boolean;
  secureDns?: boolean;
  nameservers?: string[];
  dhcpv4Start?: number;
  dhcpv4End?: number;
  /** Regulatory country — affects which channels/power levels are legal. */
  countryCode?: string;
  /** Per-band radio kill switches. */
  disable2ghz?: boolean;
  disable5ghz?: boolean;
  disable5ghzHigh?: boolean;
  /** Steers dual-band clients toward 5 GHz when signal allows; some client
   *  hardware handles the forced roam badly, hence the off switch. */
  disableBandSteering?: boolean;
  /** Relaxes duty-cycle limits meant to protect nearby homes — legitimate
   *  only where the router genuinely sits outdoors, away from neighbors. */
  outdoorMode?: boolean;
}

const NETWORK_FIELDS = new Set<keyof WifiConfigChangesJson>([
  "networkName",
  "networkName5ghz",
  "networkPassword",
  "dhcpv4Start",
  "dhcpv4End",
]);

/** field -> its apply_* flag, same proto3-JSON convention as the dish's
 *  CONFIG_APPLY_FLAG (see dishClient.ts). Excludes the NETWORK_FIELDS above,
 *  which ride under the single `applyNetworks` flag instead — confirmed
 *  against the real WifiConfig message. */
const SIMPLE_APPLY_FLAG: Record<
  Exclude<keyof WifiConfigChangesJson, "networkName" | "networkName5ghz" | "networkPassword" | "dhcpv4Start" | "dhcpv4End">,
  string
> = {
  bypassMode: "applyBypassMode",
  customDnsDisabled: "applyCustomDnsDisabled",
  secureDns: "applySecureDns",
  nameservers: "applyNameservers",
  countryCode: "applyCountryCode",
  disable2ghz: "applyDisable2ghz",
  disable5ghz: "applyDisable5ghz",
  disable5ghzHigh: "applyDisable5ghzHigh",
  disableBandSteering: "applyDisableBandSteering",
  outdoorMode: "applyOutdoorMode",
};

export interface WifiConfigRequestJson {
  targetId: string;
  wifiSetConfig: { wifiConfig: Record<string, unknown> };
}

/** `applyNetworks` replaces the router's whole `networks[0]`, BasicServiceSet
 *  array included — so an SSID/password/DHCP-range edit has to be spliced
 *  into what the router already has, never sent alone, or every band and the
 *  whole DHCP window would be wiped. This is exactly the shape `getWifiConfig()`
 *  already returns (`WifiNetworkConfigJson.networks[0]`), read fresh right
 *  before the edit so it can never be built from stale UI state. */
export interface CurrentNetworkJson {
  ipv4?: string;
  domain?: string;
  vlan?: number;
  dhcpv4LeaseDurationS?: number;
  dhcpv4Start?: number;
  dhcpv4End?: number;
  basicServiceSets?: WifiBasicServiceSetJson[];
}

const is5ghzBand = (band: string | undefined): boolean => (band ?? "").startsWith("RF_5GHZ");

/** One BasicServiceSet with the requested SSID/password spliced in. Every
 *  other field (bssid, ifaceName, band, disable, hidden...) passes through
 *  untouched — this never widens what a write can touch beyond what was
 *  actually asked for. */
function withNetworkNameAndPassword(
  set: WifiBasicServiceSetJson,
  changes: WifiConfigChangesJson,
): WifiBasicServiceSetJson {
  const next: WifiBasicServiceSetJson & { authWpa2?: { password: string } } = { ...set };
  const name5ghzOnly = changes.networkName5ghz;
  if (is5ghzBand(set.band) && name5ghzOnly !== undefined) {
    next.ssid = name5ghzOnly;
  } else if (changes.networkName !== undefined) {
    next.ssid = changes.networkName;
  }
  // Only a band currently on WPA2 gets the new password: rewriting `authWpa2`
  // on a WPA3 (or open) band would silently change its security type, which
  // nobody asked for. A band this misses simply keeps its old password.
  if (changes.networkPassword !== undefined && "authWpa2" in set) {
    next.authWpa2 = { password: changes.networkPassword };
  }
  return next;
}

function buildNetworksPatch(
  changes: WifiConfigChangesJson,
  currentNetwork: CurrentNetworkJson,
): Record<string, unknown> {
  const basicServiceSets = (currentNetwork.basicServiceSets ?? []).map((set) =>
    changes.networkName !== undefined ||
    changes.networkName5ghz !== undefined ||
    changes.networkPassword !== undefined
      ? withNetworkNameAndPassword(set, changes)
      : set,
  );
  return {
    ipv4: currentNetwork.ipv4,
    domain: currentNetwork.domain,
    vlan: currentNetwork.vlan,
    dhcpv4LeaseDurationS: currentNetwork.dhcpv4LeaseDurationS,
    dhcpv4Start: changes.dhcpv4Start ?? currentNetwork.dhcpv4Start,
    dhcpv4End: changes.dhcpv4End ?? currentNetwork.dhcpv4End,
    basicServiceSets,
  };
}

function requestFor(
  targetId: string,
  changes: WifiConfigChangesJson,
  currentNetwork: CurrentNetworkJson | undefined,
): WifiConfigRequestJson {
  if (!targetId.startsWith("Router-")) throw new Error("invalid router target id");
  const wifiConfig: Record<string, unknown> = {};

  for (const key of Object.keys(SIMPLE_APPLY_FLAG) as (keyof typeof SIMPLE_APPLY_FLAG)[]) {
    const value = changes[key];
    if (value === undefined) continue;
    wifiConfig[key] = value;
    wifiConfig[SIMPLE_APPLY_FLAG[key]] = true;
  }

  const touchesNetwork = [...NETWORK_FIELDS].some((key) => changes[key] !== undefined);
  if (touchesNetwork) {
    if (!currentNetwork)
      throw new Error("current network config is required to change the network name, password, or DHCP range");
    wifiConfig.networks = [buildNetworksPatch(changes, currentNetwork)];
    wifiConfig.applyNetworks = true;
  }

  return { targetId, wifiSetConfig: { wifiConfig } };
}

/** Trusted-host preparation, same shape as prepareDishConfigUpdate: read the
 *  router's own identity + (if needed) its current network off the LAN, then
 *  encode exactly the requested change. The encoded bytes are sent over the
 *  cloud path by the caller -- current firmware answers this RPC with
 *  grpc-status 7 (PERMISSION_DENIED) on the LAN, see LOCAL-API.md. */
export async function prepareWifiConfigUpdate(
  router: DishClient,
  changes: WifiConfigChangesJson,
): Promise<Uint8Array> {
  const deviceInfo = await router.getDeviceInfo(AbortSignal.timeout(5_000));
  const targetId = deviceInfo.id;
  if (!targetId) throw new Error("Starlink router identity is unavailable");

  let currentNetwork: CurrentNetworkJson | undefined;
  if ([...NETWORK_FIELDS].some((key) => changes[key] !== undefined)) {
    const wifiConfig = await router.getWifiConfig(AbortSignal.timeout(5_000));
    currentNetwork = wifiConfig.networks?.[0];
  }

  return router.encodeRequest(requestFor(targetId, changes, currentNetwork));
}
