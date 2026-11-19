"""
rse5.py — RSE5 (.RT) container decryptor for Reese Tunnel config files.

Algorithm fully reverse engineered from librse5.so (REESE TUNNEL 3.23.28) and
verified byte-for-byte against the original native code via ARM64 emulation:

  header   : magic A7 19 D2 4C C6 | flags byte | 16B salt | 12B nonce
  body     : AES-256-CTR ciphertext + 16B tag   (counter = nonce||BE32(2..))
  key      : 600,000-iteration HMAC-SHA256 chain over
             material = "REESE-CUSTOM/RSE5|" + hwid(if flags&1) + "|" + password,
             all outputs XOR-accumulated
  tag      : RSE5 GHASH variant (header34 + ct + lenblock, final multiply),
             tag = E(nonce||00000001) ^ Y      [NOT textbook GCM — do not swap]
  payload  : [v][v+16 rand][BE32 len][raw-DEFLATE json][rand tail]

This module is self-contained and dependency-light (pycryptodome only).
Run `python3 rse5.py` to execute the built-in self tests.
"""

import hashlib
import hmac
import json
import struct
import zlib

from Crypto.Cipher import AES

MAGIC = bytes.fromhex("a719d24cc6")
R = 0xE1000000000000000000000000000000  # GCM reduction constant


# --------------------------------------------------------------------------- #
# Key derivation                                                              #
# --------------------------------------------------------------------------- #
def build_material(flags: int, hwid: bytes, password: bytes) -> bytes:
    material = b"REESE-CUSTOM/RSE5|"
    if flags & 1:
        material += hwid.upper()
    material += b"|" + password
    return material


def kdf(material: bytes, salt: bytes) -> bytes:
    """600,000 HMAC-SHA256 chain, XOR-accumulated -> 32-byte AES-256 key."""
    sha = "sha256"
    state = hmac.digest(material, salt + b"\x00\x00\x00\x01", sha)
    acc = int.from_bytes(state, "big")
    digest = hmac.digest
    for _ in range(599_999):
        state = digest(material, state, sha)
        acc ^= int.from_bytes(state, "big")
    return acc.to_bytes(32, "big")


# --------------------------------------------------------------------------- #
# RSE5 GHASH (verified variant) — fast byte-table implementation              #
# --------------------------------------------------------------------------- #
def _mul_slow(x: int, h: int) -> int:
    """Reference multiply (bit loop) — matches native librse5.so exactly."""
    z = 0
    v = h
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        v = ((v >> 1) ^ R) if (v & 1) else (v >> 1)
    return z


class GHasher:
    """Fast X*H for a fixed H, using per-byte tables (exact, ~30x faster)."""

    def __init__(self, h_int: int):
        # P[i] = H * x^i  (i doublings, reflected convention)
        p = [0] * 128
        v = h_int
        for i in range(128):
            p[i] = v
            v = ((v >> 1) ^ R) if (v & 1) else (v >> 1)
        # B[pos][byte] = XOR of P[8*pos + 7 - m] for set bits m (m = bit value index,
        # LSB first) — the byte's LSB carries the HIGHEST x power of the byte group
        self.btab = []
        for pos in range(16):
            bits = list(reversed(p[8 * pos: 8 * pos + 8]))  # bits[m] = P[8p+7-m]
            tab = [0] * 256
            for b in range(1, 256):
                low = b & -b
                j = low.bit_length() - 1
                tab[b] = tab[b ^ low] ^ bits[j]
            self.btab.append(tab)

    def mul(self, x: int) -> int:
        z = 0
        shift = 120
        for pos in range(16):
            z ^= self.btab[pos][(x >> shift) & 0xFF]
            shift -= 8
        return z


def compute_tag(key: bytes, nonce: bytes, header: bytes, ct: bytes) -> bytes:
    """RSE5 tag: GHASH(header blocks || ct blocks || lenblock), tag = E(J0) ^ Y."""
    cipher = AES.new(key, AES.MODE_ECB)
    h_int = int.from_bytes(cipher.encrypt(b"\x00" * 16), "big")
    gh = GHasher(h_int)

    y = 0
    for data in (header, ct):
        for i in range(0, len(data), 16):
            blk = data[i:i + 16]
            x = int.from_bytes(blk.ljust(16, b"\x00"), "big")
            y = gh.mul(y ^ x)

    lenblock = ((len(header) * 8) << 64) | (len(ct) * 8)
    y = gh.mul(y ^ lenblock)

    j0_ct = cipher.encrypt(nonce + b"\x00\x00\x00\x01")
    return (int.from_bytes(j0_ct, "big") ^ y).to_bytes(16, "big")


# --------------------------------------------------------------------------- #
# CTR (counter starts at nonce||BE32(2))                                      #
# --------------------------------------------------------------------------- #
def ctr_crypt(key: bytes, nonce: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    cipher = AES.new(key, AES.MODE_ECB)
    n_blocks = (len(data) + 15) // 16
    stream = b"".join(
        nonce + struct.pack(">I", 2 + i) for i in range(n_blocks)
    )
    ks = cipher.encrypt(stream)
    return bytes(a ^ b for a, b in zip(data, ks))


# --------------------------------------------------------------------------- #
# Container                                                                   #
# --------------------------------------------------------------------------- #
def strip_padding(pt: bytes) -> bytes:
    """Remove the app's random padding wrapper."""
    v = pt[0]
    start = v + 21
    if start + 4 > len(pt):
        raise ValueError("padding header out of range")
    length = int.from_bytes(pt[v + 17:v + 21], "big")
    if length <= 0 or start + length > len(pt):
        raise ValueError("bad padding length")
    return pt[start:start + length]


def parse_header(rt: bytes):
    """Return (flags, salt, nonce, ct, tag) or raise ValueError."""
    if len(rt) < 50 or rt[:5] != MAGIC:
        raise ValueError("not an RSE5 file (bad magic)")
    flags = rt[5]
    salt = rt[6:22]
    nonce = rt[22:34]
    body = rt[34:]
    if len(body) < 16:
        raise ValueError("file too short")
    return flags, salt, nonce, body[:-16], body[-16:]


def decrypt(rt: bytes, password: bytes = b"", hwid: bytes = b"") -> dict:
    """Decrypt an .RT file. Raises ValueError with a friendly reason on failure."""
    flags, salt, nonce, ct, tag = parse_header(rt)
    material = build_material(flags, hwid, password)
    key = kdf(material, salt)
    if compute_tag(key, nonce, rt[:34], ct) != tag:
        raise ValueError("wrong password" if flags & 2 else "wrong hwid/key")
    pt = ctr_crypt(key, nonce, ct)
    payload = strip_padding(pt)
    envelope = zlib.decompress(payload, -15).decode("utf-8", "replace")
    try:
        obj = json.loads(envelope)
    except json.JSONDecodeError:
        obj = {"raw": envelope}
    return {"key": key, "flags": flags, "envelope": obj, "json": envelope}


# --------------------------------------------------------------------------- #
# Self tests                                                                  #
# --------------------------------------------------------------------------- #
def _selftest():
    import os
    # 1. fast GHASH table must match the verified bit-loop multiply
    for _ in range(50):
        x = int.from_bytes(os.urandom(16), "big")
        h = int.from_bytes(os.urandom(16), "big")
        assert _mul_slow(x, h) == GHasher(h).mul(x), "GHASH table mismatch"
    print("GHASH table self-test: OK (50 random vectors)")

    # 2. known-good key vector from native emulation of the real TELKOM file
    #    (material REESE-CUSTOM/RSE5||TEST123 -> key fffb8e0c...)
    rt = open("/home/z/my-project/upload/TELKOM UNLIMITED ____.RT (2)", "rb").read() \
        if os.path.exists("/home/z/my-project/upload/TELKOM UNLIMITED ____.RT (2)") else None
    if rt:
        flags, salt, nonce, ct, tag = parse_header(rt)
        key = kdf(build_material(flags, b"", b"TEST123"), salt)
        assert key.hex() == "fffb8e0cb57e82b0274e5bb8d58da25300eedc9603080d4d8ce6856b32bcb69e", key.hex()
        print("KDF known-vector self-test: OK")
        # 3. wrong password must fail the tag
        assert compute_tag(kdf(build_material(flags, b"", b"nope-nope"), salt),
                           nonce, rt[:34], ct) != tag
        print("wrong-password rejection self-test: OK")
    else:
        print("KDF known-vector self-test: SKIPPED (sample .RT not present)")


if __name__ == "__main__":
    _selftest()
