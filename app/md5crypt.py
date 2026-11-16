"""
md5crypt ($1$) — implementace podle původního FreeBSD algoritmu (Poul-Henning Kamp).
Webshare API vyžaduje heslo jako sha1(md5crypt(heslo, salt)) — viz apidoc.
Vlastní implementace, abychom nezáviseli na passlib/crypt (crypt modul zmizel v Pythonu 3.13).
"""
import hashlib

_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def _to64(v: int, n: int) -> str:
    out = ""
    while n > 0:
        out += _ITOA64[v & 0x3F]
        v >>= 6
        n -= 1
    return out


def md5crypt(password: str, salt: str, magic: str = "$1$") -> str:
    pw = password.encode("utf-8")
    salt = salt[:8].encode("utf-8")
    m = magic.encode("utf-8")

    ctx = hashlib.md5(pw + m + salt)
    alt = hashlib.md5(pw + salt + pw).digest()

    n = len(pw)
    while n > 0:
        ctx.update(alt[: min(16, n)])
        n -= 16

    i = len(pw)
    while i:
        ctx.update(b"\x00" if i & 1 else pw[:1])
        i >>= 1

    final = ctx.digest()

    for i in range(1000):
        c = hashlib.md5()
        c.update(pw if i & 1 else final)
        if i % 3:
            c.update(salt)
        if i % 7:
            c.update(pw)
        c.update(final if i & 1 else pw)
        final = c.digest()

    f = final
    result = ""
    result += _to64((f[0] << 16) | (f[6] << 8) | f[12], 4)
    result += _to64((f[1] << 16) | (f[7] << 8) | f[13], 4)
    result += _to64((f[2] << 16) | (f[8] << 8) | f[14], 4)
    result += _to64((f[3] << 16) | (f[9] << 8) | f[15], 4)
    result += _to64((f[4] << 16) | (f[10] << 8) | f[5], 4)
    result += _to64(f[11], 2)

    return magic + salt.decode("utf-8") + "$" + result
