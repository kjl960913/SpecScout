"""Error message templates: exact bytes, with whole-word echoes of argv replaced by slots."""


def _word(c):
    return chr(c).isalnum() or c == ord("_")


def template(data, argv):
    """bytes -> list of parts: {"literal": str} | {"argv": index}. Reconstructs exactly."""
    tokens = {}
    for i, tok in enumerate(argv):
        b = tok.encode("utf-8", "surrogateescape")
        if len(b) >= 2 and b not in tokens:
            tokens[b] = i
    choices = sorted(tokens, key=lambda b: (-len(b), b))

    def whole(b, at):
        end = at + len(b)
        if _word(b[0]) and at > 0 and _word(data[at - 1]):
            return False
        # '-length' inside '--length' is the program's own spelling, not an echo of the argument
        if b[:1] == b"-" and at > 0 and data[at - 1:at] == b"-":
            return False
        return not (_word(b[-1]) and end < len(data) and _word(data[end]))

    parts, lit, i = [], bytearray(), 0
    while i < len(data):
        m = next((b for b in choices if data.startswith(b, i) and whole(b, i)), None)
        if m is None:
            lit.append(data[i])
            i += 1
            continue
        if lit:
            parts.append({"literal": bytes(lit).decode("utf-8", "surrogateescape")})
            lit.clear()
        parts.append({"argv": tokens[m]})
        i += len(m)
    if lit or not parts:
        parts.append({"literal": bytes(lit).decode("utf-8", "surrogateescape")})
    return parts


def render(parts):
    return "".join(p["literal"] if "literal" in p else "{argv[%d]}" % p["argv"] for p in parts)


def expand(parts, argv):
    return "".join(p["literal"] if "literal" in p else argv[p["argv"]] for p in parts)


def error_key(rec, blobs):
    """(exit, stream, rendered template) for an error record. Numbers that vary between runs show as <N>."""
    from execute import mask_numbers
    err = blobs.get(rec["stderr_sha"]) or b""
    stream, data = ("stderr", err) if err else ("stdout", blobs.get(rec["stdout_sha"]) or b"")
    argv = rec["argv"]
    if rec.get("masked"):
        data = mask_numbers(data)
        argv = [mask_numbers(a.encode("utf-8", "surrogateescape")).decode("utf-8", "surrogateescape") for a in argv]
    return rec["exit"], stream, render(template(data, argv))
