#!/usr/bin/env python3
# bump_versions.py -- propagate human-cut anyvm-org/<os>-builder releases
# into this repo: bump DEFAULT_BUILDER_VERSIONS in anyvm.py, extend the
# per-OS test workflow matrices, and mirror coverage.allow suffix rules
# onto new tags.
#
# Run from the anyvm repo root:
#   python3 .github/bump_versions.py [--check]
#
# --check prints what it would do and never touches the disk.
#
# Design: docs/superpowers/specs/2026-07-30-version-bump-bots-design.md in
# the anyvm-org tree (Bot 2). Every edit is TEXTUAL and line-scoped:
# the DEFAULT_BUILDER_VERSIONS entry line, the matrix `release: [...]`
# line, and appended coverage.allow lines. Comments, matrix excludes and
# everything else survive byte-identically; an unrecognized shape is
# skipped with a warning, never guessed at. coverage.yml independently
# re-checks the result, so a missed surface goes red with a name, not
# silent.

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

ANYVM_PY = "anyvm.py"
WF_DIR = os.path.join(".github", "workflows")
ALLOW = os.path.join(".github", "coverage.allow")
BUILDER_ORG = "anyvm-org"
API = "https://api.github.com"

DEFAULT_RE = re.compile(
    r'^(\s*)"([a-z0-9]+)":\s*"([0-9][0-9.]*)",?\s*$')
RELEASE_LINE_RE = re.compile(r'^(\s*)release:\s*\[([^\]]*)\]\s*$')
ARCH_LINE_RE = re.compile(r'^(\s*)arch:\s*\[([^\]]*)\]\s*$')


class FetchError(Exception):
    pass


def log(msg):
    sys.stdout.write("bump: %s\n" % msg)


def warn(msg):
    sys.stderr.write("bump: WARNING: %s\n" % msg)


def natural_key(s):
    key = []
    for tok in re.split(r"[.\-_]", s):
        for part in re.findall(r"\d+|\D+", tok):
            if part.isdigit():
                key.append((0, int(part), ""))
            else:
                key.append((1, 0, part.lower()))
    return key


def branch_key(release):
    """The maintenance branch of a release: the release minus its LAST
    numeric component ("10.1"/"10.2" -> 10, "11.0" -> 11). Same rule as
    base-builder gendata.branch_key, which the upstream watcher uses to
    land a point release of an older line; a single-token version
    collapses to one branch for the whole OS."""
    key = natural_key(release)
    for i in range(len(key) - 1, -1, -1):
        if key[i][0] == 0:
            return tuple(key[:i])
    return tuple(key)


def strip_v(tag):
    return tag[1:] if tag.startswith("v") else tag


def _fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "anyvm-bump-bot/1.0"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(API):
        req.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise FetchError("HTTP %d" % e.code)
    except Exception as e:
        raise FetchError(str(e))


def read_default_versions():
    """The DEFAULT_BUILDER_VERSIONS entries, parsed line by line from the
    dict literal in anyvm.py. Refuses nothing here -- an OS whose line is
    not found simply is not bumpable, and rewrite refuses per-OS."""
    out = {}
    in_dict = False
    with open(ANYVM_PY, "r", encoding="utf-8") as f:
        for line in f:
            if line.startswith("DEFAULT_BUILDER_VERSIONS"):
                in_dict = True
                continue
            if in_dict:
                if line.strip().startswith("}"):
                    break
                m = DEFAULT_RE.match(line)
                if m:
                    out[m.group(2)] = m.group(3)
    return out


def rewrite_default_version(osname, new):
    """Rewrite one `"<os>": "<ver>",` line inside the dict. Line-scoped:
    an OS with no existing line is NOT added (the dict is hand-curated;
    inventing entries is not this bot's call)."""
    with open(ANYVM_PY, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    lines = text.splitlines(True)
    in_dict = False
    hit = False
    out = []
    for line in lines:
        if line.startswith("DEFAULT_BUILDER_VERSIONS"):
            in_dict = True
        elif in_dict and line.strip().startswith("}"):
            in_dict = False
        elif in_dict:
            m = DEFAULT_RE.match(line)
            if m and m.group(2) == osname:
                eol = "\r\n" if line.endswith("\r\n") else "\n"
                comma = "," if line.rstrip().endswith(",") else ""
                line = '%s"%s": "%s"%s%s' % (m.group(1), osname, new,
                                             comma, eol)
                hit = True
        out.append(line)
    if hit:
        with open(ANYVM_PY, "w", encoding="utf-8", newline="") as f:
            f.write("".join(out))
    return hit


def _parse_list(raw):
    return [t.strip().strip('"') for t in raw.split(",") if t.strip()]


def _bases_of(index):
    """Base releases (non-desktop, build:true, not a hyphen-extension of
    another release)."""
    rels = set(e["release"] for e in index
               if e.get("build", True) and not e.get("desktop"))
    return set(r for r in rels
               if not any(o != r and r.startswith(o + "-") for o in rels))


def _base_of(release, bases):
    if release in bases:
        return release
    cands = [b for b in bases if release.startswith(b + "-")]
    return max(cands, key=len) if cands else release


def _is_new(base, curbases, maxbase):
    """Whether a tracking job whose bases are `curbases` should gain
    `base`: it is newer than everything the job lists, or newer than the
    job's newest release on base's own branch. The second arm is a point
    release of an older line cut after a newer major -- FreeBSD 14.5
    after 15.1, NetBSD 9.5 and 10.2 after 11.0. Comparing only against
    the newest release skipped those silently, with no note, and
    coverage.yml went red on each. A branch the job does not list at
    all is never revived, and a release below the job's newest on its
    branch is never back-filled: both are hand-curated gaps."""
    if base in curbases:
        return False
    if natural_key(base) > natural_key(maxbase):
        return True
    same = [natural_key(r) for r in curbases
            if branch_key(r) == branch_key(base)]
    return bool(same) and natural_key(base) > max(same)


def extend_matrices(osname, index):
    """Add new releases to `<os>.yml` matrix release lists.

    A job's matrix is recognized by its `release: [...]` line. An
    `arch: [...]` line at the same indent, within the same matrix block,
    names the arches the release must ship for; a job that has none
    (plan9.yml lists release + runs and carries its own steps instead of
    calling testrun.yml) is single-arch by construction and is read as
    x86_64, with a note. Three refusals keep hand-curated jobs safe:

    - a list that is empty or contains "" is a SENTINEL (freebsd's
      cross-host powerpc64 job uses release: [""] to mean "the default
      release only") and is never touched;
    - a job whose newest base release is OLDER than the file-wide newest
      is FROZEN (openbsd's testold deliberately pins 7.3-7.6) and is
      never touched -- only jobs already tracking the newest release
      keep tracking it;
    - a release missing on any of the job's arches ("" = x86_64) is not
      added and is named in the notes: the hand-written exclude that
      would make it fit stays a human call.

    A tracking job gains every release newer than all it lists, plus a
    point release of any branch it already lists (10.2 next to 10.0 /
    10.1 in a job that also runs 11.0) -- see _is_new(). Each addition
    is inserted after the last listed release at or below it, so the
    list stays in release order and its existing entries keep theirs.

    Variant members mirror the current list: if the job lists
    `26.1-xfce` next to `26.1` (ghostbsd's real matrix), a new base
    `27.0` brings `27.0-xfce` along when the index ships it -- desktop
    or not; the job's own content defines its variant policy.

    Only the release line's bracketed list is rewritten; every other
    byte of the file survives. Returns (changed, notes).
    """
    path = os.path.join(WF_DIR, "%s.yml" % osname)
    notes = []
    if not os.path.exists(path):
        # Not a warning to swallow: it means NO matrix tracks this OS at
        # all, so its new releases are untested until someone writes the
        # workflow. It travels in notes so the notification issue says so.
        notes.append("no %s -- this OS has no per-OS workflow, so nothing "
                     "tracks its releases" % path)
        warn(notes[-1])
        return (False, notes)
    bases = _bases_of(index)
    shipped = {}
    for e in index:
        if e.get("build", True):
            shipped.setdefault(e["release"], set()).add(e["arch"])
    with open(path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    lines = text.splitlines(True)
    jobs = []
    for i, line in enumerate(lines):
        m = RELEASE_LINE_RE.match(line.rstrip("\r\n"))
        if not m:
            continue
        indent, raw = m.group(1), m.group(2)
        arches = None
        for j in range(i + 1, min(i + 8, len(lines))):
            am = ARCH_LINE_RE.match(lines[j].rstrip("\r\n"))
            if am and am.group(1) == indent:
                arches = [a if a else "x86_64"
                          for a in _parse_list(am.group(2))]
                break
        if arches is None:
            # No arch axis: a bespoke job (plan9.yml -- release + runs,
            # own steps rather than testrun.yml) that is single-arch by
            # construction, so read it as the x86_64 the index spells
            # out. Skipping it -- the behaviour until 2026-08-03 -- left
            # plan9 tracking 11554 while its builder had moved to 11952:
            # the warning went to stderr, the job stayed green, and a
            # re-run never revisits it because extend_matrices() only
            # runs on the pass that moves the pin. Noted, not silent.
            arches = ["x86_64"]
            notes.append("%s:%d: job has no arch axis; assumed x86_64"
                         % (path, i + 1))
        current = _parse_list(raw)
        if not current or "" in current:
            # sentinel list (default-release-only job); never touched
            continue
        jobs.append({"i": i, "indent": indent, "current": current,
                     "arches": arches,
                     "maxbase": max((_base_of(r, bases) for r in current),
                                    key=natural_key)})
    if not jobs:
        return (False, notes)
    filemax = max((j["maxbase"] for j in jobs), key=natural_key)
    changed = False
    for job in jobs:
        if job["maxbase"] != filemax:
            # frozen job: deliberately pinned below the file's newest
            continue
        current, arches = job["current"], job["arches"]
        curbases = set(_base_of(r, bases) for r in current)
        suffixes = [r[len(job["maxbase"]):] for r in current
                    if r != job["maxbase"]
                    and r.startswith(job["maxbase"] + "-")]
        merged = list(current)
        for b in sorted((b for b in bases
                         if _is_new(b, curbases, job["maxbase"])),
                        key=natural_key):
            missing = [a for a in arches if a not in shipped.get(b, set())]
            if missing:
                notes.append("%s: not added to the %s job: no %s image"
                             % (b, "/".join(arches), "/".join(missing)))
                continue
            group = [b]
            for suf in suffixes:
                v = b + suf
                if v in merged or v in group:
                    continue
                vmissing = [a for a in arches
                            if a not in shipped.get(v, set())]
                if vmissing:
                    notes.append("%s: not added to the %s job: no %s image"
                                 % (v, "/".join(arches),
                                    "/".join(vmissing)))
                else:
                    group.append(v)
            # after the last listed release at or below b: 10.2 lands
            # beside 10.1, not after 11.0; a release newer than
            # everything is appended as before
            pos = 0
            for k, r in enumerate(merged):
                if natural_key(_base_of(r, bases)) <= natural_key(b):
                    pos = k + 1
            merged[pos:pos] = group
        if merged != current:
            i = job["i"]
            eol = "\r\n" if lines[i].endswith("\r\n") else "\n"
            lines[i] = '%srelease: [%s]%s' % (
                job["indent"],
                ", ".join('"%s"' % r for r in merged), eol)
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write("".join(lines))
    return (changed, notes)


def mirror_allow_lines(osname, new_tags):
    """Append `<os> <tag> <wf>` allow lines for new tags whose hyphen
    suffix matches an existing line's -- the same rule as watch.py's
    suggest_allow_lines. The bot never invents a new exemption."""
    if not os.path.exists(ALLOW):
        return []
    with open(ALLOW, "r", encoding="utf-8") as f:
        text = f.read()
    existing = []
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        parts = body.split()
        if len(parts) == 3 and parts[0] == osname and parts[1] != "*":
            existing.append((parts[1], parts[2]))
    added = []
    for tag in new_tags:
        for old_tag, wf in existing:
            suffix = old_tag.split("-", 1)[1] if "-" in old_tag else ""
            if suffix and tag != old_tag and tag.endswith("-" + suffix):
                line = "%s %s %s" % (osname, tag, wf)
                if line not in text and line not in added:
                    added.append(line)
    if added:
        with open(ALLOW, "a", encoding="utf-8", newline="\n") as f:
            if not text.endswith("\n"):
                f.write("\n")
            # keep the appended block attributable: without a header these
            # lines would visually merge into whatever hand-written
            # comment block happens to sit at end of file
            f.write("\n# Mirrored by bump_versions.py (same suffix rule "
                    "as the lines cited above).\n")
            for line in added:
                f.write(line + "\n")
    return added


def fetch_release_index(osname, builder, fetch):
    url = ("https://github.com/%s/%s-builder/releases/download/v%s/"
           "releases.json" % (BUILDER_ORG, osname, builder))
    return json.loads(fetch(url).decode("utf-8"))["releases"]


def latest_release_tag(repo, fetch):
    data = fetch("%s/repos/%s/releases/latest" % (API, repo))
    return strip_v(json.loads(data.decode("utf-8"))["tag_name"])


def main(argv=None, fetch=_fetch):
    ap = argparse.ArgumentParser(
        description="propagate builder releases into anyvm")
    ap.add_argument("--check", action="store_true",
                    help="print the plan; write nothing")
    ap.add_argument("--landed-out",
                    help="write a summary line per bumped OS here, so the "
                         "workflow can open the notification issue (never "
                         "written on --check or a no-op run)")
    ap.add_argument("--notes-out",
                    help="write the matrix notes here -- assumptions the "
                         "bot made and releases it would not add on its "
                         "own -- so the notification issue carries them "
                         "instead of leaving them in the run log")
    args = ap.parse_args(argv)
    if not os.path.exists(ANYVM_PY):
        sys.stderr.write("bump: no anyvm.py here; run from the repo root\n")
        return 1

    versions = read_default_versions()
    if not versions:
        sys.stderr.write("bump: DEFAULT_BUILDER_VERSIONS not found\n")
        return 1

    rc = 0
    all_notes = []
    landed = []
    for osname in sorted(versions):
        cur = versions[osname]
        try:
            latest = latest_release_tag(
                "%s/%s-builder" % (BUILDER_ORG, osname), fetch)
        except FetchError as e:
            warn("cannot read %s-builder latest release: %s" % (osname, e))
            rc = 1
            continue
        if natural_key(latest) <= natural_key(cur):
            log("%s %s is current (latest %s)" % (osname, cur, latest))
            continue
        log("%s %s -> %s" % (osname, cur, latest))
        try:
            index = fetch_release_index(osname, latest, fetch)
        except FetchError as e:
            sys.stderr.write(
                "bump: %s v%s has no releases.json asset (%s) -- "
                "not bumping this OS\n" % (osname, latest, e))
            rc = 1
            continue
        if args.check:
            log("would set DEFAULT_BUILDER_VERSIONS[%r] = %r"
                % (osname, latest))
            continue
        if not rewrite_default_version(osname, latest):
            warn("%s: no DEFAULT_BUILDER_VERSIONS line found, skipped"
                 % osname)
            rc = 1
            continue
        log("DEFAULT_BUILDER_VERSIONS[%r] = %r" % (osname, latest))
        landed.append("%s %s -> %s" % (osname, cur, latest))
        changed, notes = extend_matrices(osname, index)
        if changed:
            log("extended %s.yml matrices" % osname)
        for n in notes:
            log("  " + n)
        all_notes.extend("%s: %s" % (osname, n) for n in notes)
        new_tags = [e["tag"] for e in index
                    if e.get("build", True) and not e.get("desktop")]
        added = mirror_allow_lines(osname, new_tags)
        for line in added:
            log("coverage.allow += %s" % line)
    if all_notes:
        # Assumptions the bot made and releases it refused to add. Both
        # need a human eye, and both used to live only in the run log.
        log("Matrix notes (confirm these by hand):")
        for n in all_notes:
            log("  " + n)
        if args.notes_out and not args.check:
            with open(args.notes_out, "w", encoding="utf-8",
                      newline=chr(10)) as f:
                f.write(chr(10).join(all_notes) + chr(10))
    if landed and args.landed_out and not args.check:
        # A silent success is a miss: the maintainer should hear which
        # defaults moved (and cut an anyvm release when convenient so CLI
        # users get the new pins). The workflow turns this file into a
        # notification issue.
        with open(args.landed_out, "w", encoding="utf-8",
                  newline=chr(10)) as f:
            f.write(chr(10).join(landed) + chr(10))
    return rc


if __name__ == "__main__":
    sys.exit(main())
