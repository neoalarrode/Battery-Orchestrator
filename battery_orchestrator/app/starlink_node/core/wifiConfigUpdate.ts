import type { DishClient } from "./dishClient";

/** The subset of the router's writable WiFi/network fields this integration
 *  exposes. Mirrors the simple fields Starlink's own app edits (network name,
 *  password, security) plus the handful of network-level toggles the user
 *  asked for by name (DNS, bypass, DHCP range). Deliberately NOT exhaustive —
 *  see PATCH.md for the full field list the schema actually supports. */
export interface WifiConfigChangesJson {
  networkName?: string;
  networkName5ghz?: string;
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

/** field -> its apply_* flag, same proto3-JSON convention as the dish's
 *  CONFIG_APPLY_FLAG (see dishClient.ts) -- confirmed against the real
 *  dish.protoset shipped in this build (see PATCH.md). dhcpv4Start/End ride
 *  under `applyNetworks`, since the DHCP range lives inside the `networks[]`
 *  array rather than as its own top-level field. */
const SIMPLE_APPLY_FLAG: Record<
  Exclude<keyof WifiConfigChangesJson, "dhcpv4Start" | "dhcpv4End">,
  string
> = {
  networkName: "applyNetworkName",
  networkName5ghz: "applyNetworkName5ghz",
  networkPassword: "applyNetworkPassword",
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

/** The current network's ipv4 CIDR + DHCP window is only known by reading it
 *  first — unlike the simple fields, `applyNetworks` replaces the whole array,
 *  so a start/end edit has to be spliced into the network the router already
 *  has, not sent alone. */
export interface CurrentNetworkJson {
  ipv4?: string;
  domain?: string;
  vlan?: number;
  dhcpv4LeaseDurationS?: number;
  dhcpv4Start?: number;
  dhcpv4End?: number;
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

  if (changes.dhcpv4Start !== undefined || changes.dhcpv4End !== undefined) {
    if (!currentNetwork) throw new Error("current network config is required to change the DHCP range");
    wifiConfig.networks = [
      {
        ipv4: currentNetwork.ipv4,
        domain: currentNetwork.domain,
        vlan: currentNetwork.vlan,
        dhcpv4LeaseDurationS: currentNetwork.dhcpv4LeaseDurationS,
        dhcpv4Start: changes.dhcpv4Start ?? currentNetwork.dhcpv4Start,
        dhcpv4End: changes.dhcpv4End ?? currentNetwork.dhcpv4End,
      },
    ];
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
  if (changes.dhcpv4Start !== undefined || changes.dhcpv4End !== undefined) {
    const wifiConfig = await router.getWifiConfig(AbortSignal.timeout(5_000));
    currentNetwork = wifiConfig.networks?.[0];
  }

  return router.encodeRequest(requestFor(targetId, changes, currentNetwork));
}
