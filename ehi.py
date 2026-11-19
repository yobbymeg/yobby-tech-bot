"""
ehi.py — HTTP Injector (.ehi) config decryptor.

Implements BOTH known schemes, auto-detected:

  Scheme B1 (HTTP Injector ~5.x era):
    AES-256-CBC (3 app keys x 7 IV constants) -> ':'-split, last segment
    -> AES-128-CBC (2 keys x 7 IVs) -> JSON prefix fix ("configSalt")
    -> field deobfuscation: reverse + custom-base64 + XOR(configSalt)

  Scheme B2 (HTTP Injector 6.3+):
    AES-256-CBC fixed L1 key (6 IV candidates) -> "p0:p1:p2" colon-split
    -> AES-128-CBC(p2, IV=b64(p0), static L2 key) -> XXTEA -> outer JSON
    -> bypass IVs: config is final; standard IVs: configData XOR layer ->
       base64 -> Argon2id (params from payload header) -> XChaCha20-Poly1305
    -> inner field decode (XOR layer / EHIMSG Java-char transform)

Key material and offsets come from public community reverse-engineering
(Pancho7532/HCDecryptor keyFile.json; @HABIBI_1ST / @NullptrO B2 findings,
"independent APK findings" cross-checked in kralonur/multi-vpn-decryptor-rs
and TisoneK/InjectX). Verified end-to-end on real production .ehi files.

Public interface:
  parse_header(raw) -> header info dict (raises ValueError)
  decrypt(raw)      -> {"scheme", "header", "config", "normalized", "json"}
"""

import base64
import binascii
import hashlib
import json
import struct

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

try:
    from argon2.low_level import hash_secret_raw, Type as Argon2Type
    ARGON2_AVAILABLE = True
except ImportError:
    ARGON2_AVAILABLE = False

# --------------------------------------------------------------------------- #
# Binary header                                                                #
# --------------------------------------------------------------------------- #
# [u16 len][magic "ehi"|"ehil"][8 bytes: fmt version + misc]
# [u16 len][app version][8 bytes: timestamps etc.]
# [u32 BE payload length][8 bytes misc][payload]


def parse_header(raw: bytes) -> dict:
    """Parse the EHI binary header, return info + encrypted payload."""
    if len(raw) < 40:
        raise ValueError("file too small for an .ehi config")
    off = 0

    def r_utf16():
        nonlocal off
        if off + 2 > len(raw):
            raise ValueError("truncated .ehi header")
        (n,) = struct.unpack(">H", raw[off:off + 2])
        off += 2
        s = raw[off:off + n]
        off += n
        return s.decode("utf-8", "ignore")

    magic = r_utf16()
    if magic not in ("ehi", "ehil"):
        raise ValueError("not an HTTP Injector config (bad magic)")
    fmt_ver, misc1 = struct.unpack(">II", raw[off:off + 8])
    off += 8
    app_version = r_utf16()
    ts_a, ts_b = struct.unpack(">Q", raw[off:off + 8])[0], None
    off += 8
    (p_len,) = struct.unpack(">I", raw[off:off + 4])
    off += 4
    off += 8  # trailing header misc
    payload = raw[off:off + p_len]
    if len(payload) != p_len:
        raise ValueError("truncated .ehi payload")
    return {
        "magic": magic,
        "lite": magic == "ehil",
        "format_version": fmt_ver,
        "app_version": app_version,
        "header_size": off,
        "payload_len": p_len,
        "payload": payload,
        "file_size": len(raw),
    }


# --------------------------------------------------------------------------- #
# Shared helpers                                                               #
# --------------------------------------------------------------------------- #
EHI_CUSTOM_ALPHABET = "RkLC2QaVMPYgGJW/A4f7qzDb9e+t6Hr0Zp8OlNyjuxKcTw1o5EIimhBn3UvdSFXs"
STD_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_B2_TRANS = str.maketrans(EHI_CUSTOM_ALPHABET, STD_ALPHABET)


def _custom_b64_b2(text: str) -> bytes:
    clean = text.replace("?", "")
    if (rem := len(clean) % 4):
        clean += "=" * (4 - rem)
    return base64.b64decode(clean.translate(_B2_TRANS))


def _aes_cbc_unpad(data: bytes, key: bytes, iv: bytes) -> bytes:
    """Tolerant CBC decrypt + PKCS7 strip (used by B1)."""
    dec = AES.new(key, AES.MODE_CBC, iv).decrypt(data)
    pad = dec[-1]
    if 1 <= pad <= 16 and all(b == pad for b in dec[-pad:]):
        dec = dec[:-pad]
    return dec


def _xxtea_decrypt(data: bytes, key: bytes) -> bytes:
    if not data:
        return b""
    if (rem := len(data) % 4):
        data += b"\x00" * (4 - rem)
    k = struct.unpack("<4I", key.ljust(16, b"\x00")[:16])
    n = len(data) // 4
    v = list(struct.unpack(f"<{n}I", data))
    delta = 0x9E3779B9
    total = ((6 + 52 // n) * delta) & 0xFFFFFFFF
    y = v[0]
    while total != 0:
        e = (total >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ \
                 ((total ^ y) + (k[(p & 3) ^ e] ^ z))
            y = v[p] = (v[p] - mx) & 0xFFFFFFFF
        z = v[n - 1]
        mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ \
             ((total ^ y) + (k[(0 & 3) ^ e] ^ z))
        y = v[0] = (v[0] - mx) & 0xFFFFFFFF
        total = (total - delta) & 0xFFFFFFFF
    out = struct.pack(f"<{n}I", *v)
    length = v[-1]
    return out[:length] if 0 < length <= n * 4 else out.rstrip(b"\x00")


# --------------------------------------------------------------------------- #
# Scheme B2 (HTTP Injector 6.3+)                                               #
# --------------------------------------------------------------------------- #
B2_L1_KEY = bytes.fromhex(
    "7e1210f7aab956f7a668bda6e57feddb7f84ad840aef8d27b1b969959be3ab6c")
B2_L2_KEY = bytes.fromhex("b2bc617c32d8b9eb1943a5ffa8051eea")
B2_XXTEA_KEY = b"null=V5kU5+FFrY\x00"
B2_BYPASS_IVS = (
    bytes.fromhex("221d572349555f1d112133236b1f4a3f"),
    bytes.fromhex("5543494c53443e3f4a6a4539384e776a"),
    bytes.fromhex("374c2541575e4d531a3c327b75431e5f"),
)
B2_STANDARD_IVS = (
    bytes.fromhex("2c5d1147bbad422b3b334d4d235f1a53"),
    bytes.fromhex("522b01433a5e8b2fc7549e1ad368e541"),
    bytes.fromhex("337a1035aaedf3458ca167e92d74b839"),
)
EHIMSG_KEY = "EHIMSG"
DEFAULT_SALT = "EVZJNI"

# master-key field groups (APK findings, Rust reference)
MK_OPTIONAL = ("configAesKey", "configIdentifier", "configSalt")
MK_TIMESTAMPS = ("configTimestamp", "configExpiryTimestamp")
MK_LOCK = ("lockModes", "lockModesHash", "configHwid",
           "configLockMobileOperatorId")


def _json_to_material(v):
    """Reproduce the APK's string spellings for master-key generation."""
    if v is None:
        return "None"
    if v is True:
        return "True"
    if v is False:
        return "False"
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)):
        return json.dumps(v)
    return str(v)


def _has_content(v):
    if v is None or isinstance(v, (dict, list)):
        return False
    if isinstance(v, str):
        return v != ""
    return True


def _b2_master_key(config: dict) -> bytes:
    material = ""
    for f in MK_OPTIONAL:
        v = config.get(f)
        if _has_content(v):
            material += _json_to_material(v)
    for f in MK_TIMESTAMPS:
        v = config.get(f)
        material += _json_to_material(v) if v is not None else "0"
    for f in MK_LOCK:
        v = config.get(f)
        if _has_content(v):
            material += _json_to_material(v)
    return hashlib.sha256(material.encode("utf-8")).digest()


def _b2_xor_layer(text: str, key: str):
    """Custom-b64(reverse) -> hex -> bytes -> XOR(key), zero bytes dropped."""
    if not text or not text.strip() or not key:
        return None if text and text.strip() else text
    try:
        hex_raw = _custom_b64_b2(text[::-1]).decode("ascii")
        if len(hex_raw) % 2:
            hex_raw = "0" + hex_raw
        raw = bytes.fromhex(hex_raw)
        k = key.encode("utf-8")
        out = bytes(b ^ k[i % len(k)] for i, b in enumerate(raw)
                    if (b ^ k[i % len(k)]) != 0)
        plain = out.decode("utf-8")
        bad = sum(1 for c in plain if ord(c) < 32 and c not in "\t\n\r")
        if plain and bad / len(plain) > 0.5:
            return None
        return plain
    except (UnicodeDecodeError, binascii.Error, ValueError):
        return None


def _b2_decode_message(text: str) -> str:
    """configMessage: base64 -> java chars (UTF-16) XOR 'EHIMSG'."""
    if not text or not text.strip():
        return text
    try:
        padded = text + "=" * ((4 - len(text) % 4) % 4)
        raw = base64.b64decode(padded)
        utf8 = raw.decode("utf-8", "replace")
        units = utf8.encode("utf-16-be", "surrogatepass")
        n = len(units) // 2
        chars = struct.unpack(f">{n}H", units)
        key = [ord(c) for c in EHIMSG_KEY]
        xored = struct.pack(f">{n}H",
                            *(c ^ key[i % len(key)] for i, c in enumerate(chars)))
        return xored.decode("utf-16-be", "replace")
    except (binascii.Error, struct.error, UnicodeDecodeError):
        return text


def _b2_decrypt(payload: bytes):
    """Return (config_dict, iv_label) or None."""
    for iv in B2_BYPASS_IVS + B2_STANDARD_IVS:
        try:
            l1 = unpad(AES.new(B2_L1_KEY, AES.MODE_CBC, iv).decrypt(payload), 16)
            text = l1.decode("utf-8", "strict")
            parts = text.split(":")
            if len(parts) < 3:
                continue
            inner_iv = base64.b64decode(parts[0])
            if len(inner_iv) != 16:
                continue
            garbage = unpad(
                AES.new(B2_L2_KEY, AES.MODE_CBC, inner_iv).decrypt(
                    base64.b64decode(parts[2])), 16)
            final = _xxtea_decrypt(garbage, B2_XXTEA_KEY)
            start = final.find(b"{")
            if start == -1:
                continue
            config = json.loads(final[start:].decode("utf-8", "ignore"))
            if not isinstance(config, dict):
                continue
            label = "bypass" if iv in B2_BYPASS_IVS else "standard"
            return config, label, iv
        except (ValueError, KeyError, struct.error, binascii.Error,
                UnicodeDecodeError):
            continue
    return None


def _b2_unlock_locked(config: dict):
    """Standard-IV path: configData -> XOR -> Argon2id -> XChaCha20-Poly1305."""
    if not ARGON2_AVAILABLE:
        raise ValueError("argon2-cffi not installed — cannot unlock locked EHI")
    salt = config.get("configSalt") or DEFAULT_SALT
    data = config.get("configData")
    if not data:
        raise ValueError("locked config without configData")
    xor_out = _b2_xor_layer(data, salt)
    if not xor_out:
        raise ValueError("configData XOR layer failed")
    raw = base64.b64decode(xor_out)
    if len(raw) <= 50:
        raise ValueError("locked payload too short")
    time_cost = int.from_bytes(raw[1:5], "little")
    memory_cost = int.from_bytes(raw[5:9], "little")
    parallelism = raw[9]
    if memory_cost > 64 * 1024 or time_cost > 8 or parallelism > 8:
        raise ValueError("argon2 work factors out of app range")
    key = hash_secret_raw(
        secret=_b2_master_key(config),
        salt=raw[0x0A:0x1A],
        time_cost=time_cost,
        memory_cost=memory_cost,
        parallelism=parallelism,
        hash_len=32,
        type=Argon2Type.ID,
    )
    from Crypto.Cipher import ChaCha20_Poly1305
    cipher = ChaCha20_Poly1305.new(key=key, nonce=raw[0x1A:0x32])  # 24B = XChaCha
    cipher.update(raw[:0x1A])
    plain = cipher.decrypt_and_verify(raw[0x32:-16], raw[-16:])
    return json.loads(plain.decode("utf-8", "ignore"))


def _b2_clean(config: dict, salt: str) -> dict:
    out = {}
    for k, v in config.items():
        if k == "configMessage" and isinstance(v, str) and v.strip():
            out[k] = _b2_decode_message(v)
        elif isinstance(v, str) and v.strip():
            dec = _b2_xor_layer(v, salt)
            out[k] = dec if dec is not None else v
        else:
            out[k] = v
    # embedded JSON fields
    for f in ("v2rRawJson", "overwriteServerData"):
        raw = out.get(f)
        if isinstance(raw, str):
            try:
                s, e = raw.find("{"), raw.rfind("}")
                if s != -1 and e > s:
                    obj = json.loads(raw[s:e + 1], strict=False)
                    out[f] = json.loads(obj, strict=False) \
                        if isinstance(obj, str) else obj
            except json.JSONDecodeError:
                pass
    return out


# --------------------------------------------------------------------------- #
# Scheme B1 (HTTP Injector ~5.x)                                               #
# --------------------------------------------------------------------------- #
B1_AES256_KEYS = [
    "fhIQ96q5VvemaL2m5X/t23+ErYQK740nsblplZvjq2w=",
    "Rnjg9Slfyas+na8yGwiXx40EXr+VIZUUz+9XD0koGBE=",
    "MiUQFV2nMl1kTKbmt9+LgO0gww7ZMuzLn+fisEz4+KQ=",
]
B1_AES128_KEYS = [
    "c9zx/b+CUJBk4ACkHUlMAQ==",
    "Igeltafe0t7U6xfOkcmCZg==",
]
B1_IVS = [
    "CFHSIHTTPINISSCF", "V5HSIHTTPINISS20", "V5HSIHTTPINISS21",
    "SBHSIHTTPINISSLS", "OBHSIHTTPINIOCTO", "AYJZIHTTPINIECKC",
    "SBHSIHTTPINILITE",
]
B1_OBFUSCATED = ("host", "user", "password", "remoteProxy", "payload",
                 "sniHostname", "shadowsocksConfig", "httpObfsSettings",
                 "v2rWsPath")
_B1_CHARSET = "RkLC2QaVMPYgGJW/A4f7qzDb9e+t6Hr0Zp8OlNyjuxKcTw1o5EIimhBn3UvdSFXs?"
_B1_TRANS = str.maketrans(_B1_CHARSET[:64], STD_ALPHABET[:64])


def _b1_custom_b64(text: str):
    try:
        std = text.replace("?", "=").translate(_B1_TRANS)
        if (rem := len(std) % 4):
            std += "=" * (4 - rem)
        return base64.b64decode(std)
    except binascii.Error:
        return None


def _b1_deobfuscate(value: str, salt: str):
    """reverse -> custom b64 -> hex chars -> XOR(configSalt)."""
    decoded = _b1_custom_b64(value[::-1])
    if decoded is None:
        return None
    hexs = decoded.hex()
    pre = "".join(chr(int(hexs[i:i + 2], 16))
                  for i in range(0, len(hexs) - 1, 2))
    return "".join(chr(ord(c) ^ ord(salt[i % len(salt)]))
                   for i, c in enumerate(pre))


def _b1_decrypt(payload: bytes):
    for k1 in B1_AES256_KEYS:
        key1 = base64.b64decode(k1)
        for iv in B1_IVS:
            try:
                text = _aes_cbc_unpad(payload, key1, iv.encode()[:16]) \
                    .decode("utf-8", "strict")
            except (UnicodeDecodeError, ValueError):
                continue
            seg = text.rsplit(":", 1)[-1] if ":" in text else text
            if len(seg) <= 20:
                continue
            # stage-2 inputs: the segment may be base64 of the ciphertext
            # or the raw ciphertext bytes — try both.
            candidates = []
            try:
                candidates.append(base64.b64decode(seg + "=" * ((4 - len(seg) % 4) % 4)))
            except (binascii.Error, ValueError):
                pass
            candidates.append(seg.encode("utf-8"))
            for k2 in B1_AES128_KEYS:
                key2 = base64.b64decode(k2)
                for blob in candidates:
                    if len(blob) % 16:
                        continue
                    for iv2 in B1_IVS:
                        try:
                            text2 = _aes_cbc_unpad(blob, key2, iv2.encode()[:16]) \
                                .decode("utf-8", "strict")
                        except (UnicodeDecodeError, ValueError):
                            continue
                        if "configSalt" not in text2:
                            continue
                        for fixed in ('{"a":"' + text2[17:],
                                      '{"a"' + text2[14:]):
                            try:
                                data = json.loads(fixed)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(data, dict):
                                return data
    return None


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #
def decrypt(raw: bytes) -> dict:
    """Decrypt any .ehi file. Raises ValueError with a human reason on failure."""
    header = parse_header(raw)
    payload = header["payload"]

    b2 = _b2_decrypt(payload)
    if b2:
        outer, iv_label, _ = b2
        salt = outer.get("configSalt") or DEFAULT_SALT
        if iv_label == "bypass":
            final = outer
        else:
            try:
                final = _b2_unlock_locked(outer)
            except ValueError as e:
                raise ValueError(f"EHI v6 lock layer: {e}") from e
            salt = final.get("configSalt") or salt
        cleaned = _b2_clean(final, salt)
        scheme = "B2 (HTTP Injector 6.3+)"
    else:
        b1 = _b1_decrypt(payload)
        if not b1:
            raise ValueError("unsupported .ehi encryption variant")
        salt = b1.get("configSalt", DEFAULT_SALT)
        cleaned = dict(b1)
        for f in B1_OBFUSCATED:
            v = cleaned.get(f)
            if isinstance(v, str) and v.strip():
                dec = _b1_deobfuscate(v, salt)
                if dec:
                    cleaned[f] = dec
        scheme = "B1 (HTTP Injector 5.x)"

    return {
        "scheme": scheme,
        "header": {k: v for k, v in header.items() if k != "payload"},
        "config": cleaned,
        "normalized": normalize(cleaned),
        "json": json.dumps(cleaned, indent=2, ensure_ascii=False),
    }


def normalize(cfg: dict) -> dict:
    """Flat, card-ready view of an EHI config (matches the premium card order)."""
    n = {}
    get = lambda k: cfg.get(k) or ""
    n["payload"] = str(get("payload"))
    n["sshServer"] = str(get("sshServer"))
    n["sshPort"] = str(get("sshPort"))
    n["sshUser"] = str(get("sshUser"))
    n["sshPassword"] = str(get("sshPassword"))
    n["remoteProxy"] = str(get("remoteProxy"))
    n["remoteProxyPort"] = str(get("remoteProxyPort"))
    n["proxyURL"] = str(get("proxyURL"))
    n["sniHostname"] = str(get("sniHostname"))
    n["ssl"] = bool(cfg.get("ssl") or cfg.get("SSL"))
    n["customDns1"] = str(get("customDns1"))
    n["customDns2"] = str(get("customDns2"))
    n["connectionType"] = str(get("connectionType") or get("tunnelType"))
    n["configMessage"] = str(get("configMessage"))
    if cfg.get("v2rRawJson") and isinstance(cfg["v2rRawJson"], dict):
        n["v2ray"] = cfg["v2rRawJson"]
    # server override embedded in the file
    if cfg.get("overwriteServerData") and isinstance(cfg["overwriteServerData"], dict):
        n["overwriteServerData"] = cfg["overwriteServerData"]
    # lock / licence info
    n["lockModes"] = str(get("lockModes"))
    n["configHwid"] = str(get("configHwid"))
    n["configExpiryTimestamp"] = cfg.get("configExpiryTimestamp")
    n["configLockMobileOperatorId"] = str(get("configLockMobileOperatorId"))
    n["lockModesHash"] = str(get("lockModesHash"))
    return n
