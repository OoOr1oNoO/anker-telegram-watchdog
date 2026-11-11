"""Optional Tuya / Smart Life smart plug: on / off, a countdown and the cycle timer from the bot.

.env: TUYA_ACCESS_ID, TUYA_ACCESS_SECRET (a cloud project on iot.tuya.com with the Smart Life
account linked and IoT Core subscribed), TUYA_DEVICE_ID, TUYA_REGION (eu, us, cn, in; eu by
default). Without them the plug is off. The cycle and the countdown run in the plug itself; the bot
only changes them, so they keep working without the bot or the internet.
"""

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.request
import uuid

from .common import log

HOSTS = {"eu": "https://openapi.tuyaeu.com", "us": "https://openapi.tuyaus.com",
         "cn": "https://openapi.tuyacn.com", "in": "https://openapi.tuyain.com"}
ALL_DAYS = 0x7F
STATUS_TTL = 5  # s: a menu render and the press after it share one status request
PENDING_S = 20  # s: a sent value is shown until the cloud reports the plug's own (it lags a few seconds)


def decode_cycle(value: str) -> dict | None:
    """cycle_time: base64 of 10-byte entries (on, days mask, start and end minute, minutes on
    and off, big-endian). The first entry, None when empty or not readable."""
    try:
        raw = base64.b64decode(value or "")
    except ValueError:
        return None
    if len(raw) < 10:
        return None
    return {"on": bool(raw[0]), "days": raw[1], "start": int.from_bytes(raw[2:4], "big"),
            "end": int.from_bytes(raw[4:6], "big"), "work": int.from_bytes(raw[6:8], "big"),
            "rest": int.from_bytes(raw[8:10], "big")}


def encode_cycle(on: bool, work: int, rest: int, start: int = 0, end: int = 0, days: int = ALL_DAYS) -> str:
    """start == end: around the clock."""
    raw = bytes([1 if on else 0, days]) + b"".join(int(v).to_bytes(2, "big") for v in (start, end, work, rest))
    return base64.b64encode(raw).decode()


def parse_cycle(text: str) -> tuple[int, int, int, int] | None:
    """"15/15" or "20/10 22:00-08:00" -> (minutes on, minutes off, start, end); None if not valid."""
    m = re.fullmatch(r"\s*(\d{1,3})\s*/\s*(\d{1,3})(?:\s+(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2}))?\s*", text)
    if not m:
        return None
    work, rest = int(m[1]), int(m[2])
    if not (1 <= work <= 720 and 1 <= rest <= 720):
        return None
    if m[3] is None:
        return work, rest, 0, 0
    h1, m1, h2, m2 = (int(m[i]) for i in range(3, 7))
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        return None
    return work, rest, h1 * 60 + m1, h2 * 60 + m2


class Plug:
    """A Tuya cloud client for one plug (category cz: switch_1, countdown_1, cycle_time, cur_power)."""

    def __init__(self, access_id: str, secret: str, device: str, region: str = "eu", call=None):
        self.access_id, self.secret, self.device = access_id, secret, device
        self.host = HOSTS.get(region, HOSTS["eu"])
        self.token, self.token_until = "", 0.0
        self.cache: tuple[float, dict | None] = (0.0, None)
        self.pending: dict[str, tuple[object, float]] = {}  # code -> (value sent, when)
        self.lock = threading.Lock()
        self._http = call or self._urlopen

    @classmethod
    def from_env(cls, env) -> "Plug | None":
        ids = (env.get("TUYA_ACCESS_ID", "").strip(), env.get("TUYA_ACCESS_SECRET", "").strip(),
               env.get("TUYA_DEVICE_ID", "").strip())
        return cls(*ids, (env.get("TUYA_REGION") or "eu").strip().lower()) if all(ids) else None

    @staticmethod
    def _urlopen(method: str, url: str, headers: dict, body: bytes | None) -> dict:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())

    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
        data = json.dumps(body).encode() if body is not None else b""
        t, nonce = str(int(time.time() * 1000)), uuid.uuid4().hex
        token = self._token() if auth else ""
        to_sign = "\n".join([method, hashlib.sha256(data).hexdigest(), "", path])
        sign = hmac.new(self.secret.encode(), (self.access_id + token + t + nonce + to_sign).encode(),
                        hashlib.sha256).hexdigest().upper()
        headers = {"client_id": self.access_id, "sign": sign, "t": t, "nonce": nonce, "sign_method": "HMAC-SHA256",
                   "Content-Type": "application/json"}
        if token:
            headers["access_token"] = token
        result = self._http(method, self.host + path, headers, data if body is not None else None)
        if not result.get("success"):
            if auth and result.get("code") in (1010, 1011):  # the token expired early: once more
                self.token_until = 0
                return self._request(method, path, body)
            raise RuntimeError(f"Tuya {path}: {result.get('code')} {result.get('msg')}")
        return result

    def _token(self) -> str:
        if time.time() < self.token_until:
            return self.token
        result = self._request("GET", "/v1.0/token?grant_type=1", auth=False)["result"]
        self.token, self.token_until = result["access_token"], time.time() + int(result.get("expire_time", 3600)) - 60
        return self.token

    def status(self, fresh: bool = False, max_age: float = STATUS_TTL) -> dict | None:
        """{code: value} of the plug plus "online" (whether the cloud reaches it: when not, the
        values are old and commands are accepted but never reach the plug); None if the cloud
        is not reachable (logged)."""
        with self.lock:
            if not fresh and time.time() - self.cache[0] < max_age:
                return self._with_pending(self.cache[1])
            try:
                rows = self._request("GET", f"/v1.0/iot-03/devices/{self.device}/status")["result"]
                status = {r["code"]: r["value"] for r in rows}
                status["online"] = bool(self._request("GET", f"/v1.0/devices/{self.device}")["result"].get("online"))
            except Exception as exc:
                log.warning("Plug status failed: %s", exc)
                status = None
            self.cache = (time.time(), status)
            return self._with_pending(status)

    def _with_pending(self, status: dict | None) -> dict | None:
        """The values just sent over the reported ones, until the cloud catches up."""
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if now - v[1] < PENDING_S}
        if status is None or not self.pending:
            return status
        out = dict(status)
        for code, (value, _) in self.pending.items():
            out[code] = value
            if code == "switch_1" and not value:
                out["cur_power"] = 0
        return out

    def send(self, **values) -> bool:
        """Send commands; False also when the plug is offline (the cloud would only keep them)."""
        if not (self.status() or {}).get("online"):
            log.warning("Plug command %s not sent: the plug is offline in the Tuya cloud", values)
            return False
        with self.lock:
            try:
                self._request("POST", f"/v1.0/iot-03/devices/{self.device}/commands",
                              {"commands": [{"code": k, "value": v} for k, v in values.items()]})
                ok = True
                self.pending.update({k: (v, time.time()) for k, v in values.items()})
            except Exception as exc:
                log.warning("Plug command %s failed: %s", values, exc)
                ok = False
            self.cache = (0.0, None)  # read it again next time
            return ok

    def load(self) -> tuple[int, int | None] | None:
        """The plug's load, W, and voltage, V (for the status widgets): None when not known or offline."""
        st = self.status(max_age=60)
        if not st or not st.get("online") or st.get("cur_power") is None or not st.get("cur_voltage"):
            return None  # the widgets show both or nothing
        return (round(st["cur_power"] / 10) if st.get("switch_1") else 0), round(st["cur_voltage"] / 10)

    def set_cycle(self, on: bool, work: int | None = None, rest: int | None = None,
                  start: int | None = None, end: int | None = None) -> bool:
        """Turn the cycle on / off or change it; what is not given stays as it is."""
        cur = decode_cycle((self.status() or {}).get("cycle_time", "")) or {
            "days": ALL_DAYS, "start": 0, "end": 0, "work": 15, "rest": 15}
        return self.send(cycle_time=encode_cycle(
            on, work or cur["work"], rest or cur["rest"], cur["start"] if start is None else start,
            cur["end"] if end is None else end, cur["days"] or ALL_DAYS))
