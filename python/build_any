#!/usr/bin/env python3
"""
build_any.py - Download, build, and install an arbitrary source project.

A best-effort, OS-independent builder. It will try a curated set of common
build systems in a sensible order, and if none work, it will ask YOU for a
shell command to run, plus where the resulting binary lives.

Usage:
    python3 build_any.py <url-or-archive> [--name NAME] [--dest DIR]
                         [--prefix DIR] [--jobs N]
                         [--recipes DIR] [--no-guess]
                         [--keep-tmp] [--dry-run]

Examples:
    python3 build_any.py https://github.com/git/git/archive/refs/tags/v2.43.0.tar.gz
    python3 build_any.py ./mytool-1.2.tar.xz --name mytool --dest ~/bin
    python3 build_any.py https://example.com/foo.zip --no-guess

Exit codes:
    0  success
    1  user aborted / no recipe found and no manual command given
    2  download/extract failed
    3  build failed and user declined to retry manually
"""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

IS_WINDOWS = platform.system() == "Windows"
EXE = ".exe" if IS_WINDOWS else ""

# Colours (no-op if not a tty)
def _c(code: str, s: str) -> str:
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"

def info(msg: str)  -> None: print(_c("36", f"[i] {msg}"))
def ok(msg: str)    -> None: print(_c("32", f"[+] {msg}"))
def warn(msg: str)  -> None: print(_c("33", f"[!] {msg}"))
def err(msg: str)   -> None: print(_c("31", f"[x] {msg}"), file=sys.stderr)

def run(cmd, cwd: Optional[Path] = None, check: bool = True,
        capture: bool = False) -> subprocess.CompletedProcess:
    """Run a command, echo it, return CompletedProcess."""
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    info(f"$ {' '.join(shlex.quote(c) for c in cmd)}"
         + (f"   (cwd={cwd})" if cwd else ""))
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
    )

def have(tool: str) -> bool:
    return shutil.which(tool) is not None

def nproc() -> int:
    try:
        return os.cpu_count() or 1
    except Exception:
        return 1

# ---------------------------------------------------------------------------
# Download + extract
# ---------------------------------------------------------------------------

def download(url: str, dest_dir: Path) -> Path:
    """Download url to dest_dir, return the local file path."""
    name = url.split("?")[0].rstrip("/").split("/")[-1] or "download"
    out = dest_dir / name
    if url.startswith("file://") or Path(url).is_file():
        src = Path(url[7:] if url.startswith("file://") else url)
        info(f"Copying local file: {src}")
        shutil.copy2(src, out)
        return out
    info(f"Downloading {url}")
    try:
        with urllib.request.urlopen(url) as r, open(out, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        err(f"Download failed: {e}")
        sys.exit(2)
    return out

def extract(archive: Path, dest: Path) -> Path:
    """Extract archive into dest, return the top-level directory."""
    info(f"Extracting {archive.name}")
    dest.mkdir(parents=True, exist_ok=True)
    before = set(dest.iterdir())
    try:
        if zipfile.is_zipfile(archive):
            with zipfile.ZipFile(archive) as z:
                z.extractall(dest)
        elif tarfile.is_tarfile(archive):
            with tarfile.open(archive) as t:
                # Python 3.12+ warns about unsafe extraction; filter if available
                try:
                    t.extractall(dest, filter="data")  # type: ignore[arg-type]
                except TypeError:
                    t.extractall(dest)
        else:
            err(f"Unrecognised archive format: {archive.name}")
            sys.exit(2)
    except Exception as e:
        err(f"Extraction failed: {e}")
        sys.exit(2)

    after = set(dest.iterdir())
    new = list(after - before)
    if len(new) == 1 and new[0].is_dir():
        return new[0]
    return dest  # tarball extracted in place

# ---------------------------------------------------------------------------
# Recipe framework
# ---------------------------------------------------------------------------

# A Recipe is: (name, needs_tools, builder_fn)
# builder_fn(src: Path, prefix: Path, jobs: int) -> bool  (True == success)
Recipe = tuple[str, list[str], Callable[[Path, Path, int], bool]]

def _autotools(src: Path, prefix: Path, jobs: int) -> bool:
    if (src / "configure").exists():
        pass
    elif (src / "autogen.sh").exists():
        run(["./autogen.sh"], cwd=src)
    elif (src / "configure.ac").exists() or (src / "configure.in").exists():
        run(["autoreconf", "-fi"], cwd=src)
    else:
        return False
    run(["./configure", f"--prefix={prefix}"], cwd=src)
    run(["make", f"-j{jobs}"], cwd=src)
    run(["make", "install"], cwd=src)
    return True

def _cmake(src: Path, prefix: Path, jobs: int) -> bool:
    if not (src / "CMakeLists.txt").exists():
        return False
    build = src / "build"
    build.mkdir(exist_ok=True)
    run(["cmake", "-S", str(src), "-B", str(build),
         f"-DCMAKE_INSTALL_PREFIX={prefix}",
         "-DCMAKE_BUILD_TYPE=Release"], cwd=src)
    run(["cmake", "--build", str(build), "--parallel", str(jobs)])
    run(["cmake", "--install", str(build)])
    return True

def _meson(src: Path, prefix: Path, jobs: int) -> bool:
    if not (src / "meson.build").exists():
        return False
    build = src / "build"
    run(["meson", "setup", str(build), str(src),
         f"--prefix={prefix}", "--buildtype=release"], cwd=src)
    run(["ninja", "-C", str(build), f"-j{jobs}"])
    run(["ninja", "-C", str(build), "install"])
    return True

def _cargo(src: Path, prefix: Path, jobs: int) -> bool:
    if not (src / "Cargo.toml").exists():
        return False
    env = os.environ.copy()
    env["CARGO_BUILD_JOBS"] = str(jobs)
    subprocess.run(["cargo", "build", "--release", "--locked"],
                   cwd=str(src), check=True, env=env)
    # Install: prefer `cargo install --path .`
    run(["cargo", "install", "--path", ".", "--root", str(prefix),
         "--locked", "--force"], cwd=src)
    return True

def _go(src: Path, prefix: Path, jobs: int) -> bool:
    if not (src / "go.mod").exists():
        return False
    bindir = prefix / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["GOBIN"] = str(bindir)
    subprocess.run(["go", "build", "-o", str(bindir), "./..."],
                   cwd=str(src), check=True, env=env)
    return True

def _make(src: Path, prefix: Path, jobs: int) -> bool:
    """Generic make — checked last, only if there's a Makefile and no better hint."""
    if not (src / "Makefile").exists() and not (src / "makefile").exists():
        return False
    run(["make", f"-j{jobs}"], cwd=src)
    # Try PREFIX=; if install target doesn't exist, just copy the built binary.
    r = run(["make", f"PREFIX={prefix}", "install"], cwd=src,
            check=False, capture=True)
    if r.returncode != 0:
        warn("`make install` failed; falling back to `make PREFIX=...` "
             "(you may need to grab the binary manually).")
    return True

def _nim(src: Path, prefix: Path, jobs: int) -> bool:
    if not list(src.glob("*.nimble")):
        return False
    run(["nimble", "build", "-d:release"], cwd=src)
    return True

def _zig(src: Path, prefix: Path, jobs: int) -> bool:
    if not (src / "build.zig").exists():
        return False
    run(["zig", "build", "-Doptimize=ReleaseFast"], cwd=src)
    return True

def _python_pep517(src: Path, prefix: Path, jobs: int) -> bool:
    if not (src / "pyproject.toml").exists() and not (src / "setup.py").exists():
        return False
    run([sys.executable, "-m", "pip", "install", "--prefix", str(prefix),
         "."], cwd=src)
    return True

def _node(src: Path, prefix: Path, jobs: int) -> bool:
    if not (src / "package.json").exists():
        return False
    run(["npm", "install"], cwd=src)
    r = run(["npm", "run", "build"], cwd=src, check=False, capture=True)
    if r.returncode != 0:
        warn("npm build script failed or missing; continuing.")
    return True

# Ordered by specificity: the first that matches AND succeeds wins.
RECIPES: list[Recipe] = [
    ("cmake",   ["cmake"],                       _cmake),
    ("meson",   ["meson", "ninja"],              _meson),
    ("autotools", ["make"],                      _autotools),
    ("cargo",   ["cargo"],                       _cargo),
    ("go",      ["go"],                          _go),
    ("zig",     ["zig"],                         _zig),
    ("nim",     ["nimble"],                      _nim),
    ("python",  [sys.executable],                _python_pep517),
    ("node",    ["npm"],                         _node),
    ("make",    ["make"],                        _make),
]

def load_user_recipes(dir_: Path) -> list[Recipe]:
    """Load optional user recipes: <dir>/<name>.json with keys
       { "check": "path/to/file", "build": "cmd", "install": "cmd" }"""
    import json
    out: list[Recipe] = []
    if not dir_.is_dir():
        return out
    for f in sorted(dir_.glob("*.json")):
        try:
            spec = json.loads(f.read_text())
            check = spec.get("check")
            build = spec["build"]
            install = spec.get("install")

            def make_fn(_src: Path, _prefix: Path, _jobs: int,
                        _build=build, _install=install):
                run(_build, cwd=_src)
                if _install:
                    run(_install, cwd=_src)
                return True

            def make_check(src: Path, _c=check) -> bool:
                return (_c is None) or (src / _c).exists()

            # Wrap: only try if check passes
            def wrapped(src: Path, prefix: Path, jobs: int,
                        _fn=make_fn, _chk=make_check):
                if not _chk(src):
                    return False
                return _fn(src, prefix, jobs)

            out.append((f.stem, [], wrapped))
        except Exception as e:
            warn(f"Skipping recipe {f.name}: {e}")
    return out

def try_build(src: Path, prefix: Path, jobs: int,
              extra: list[Recipe]) -> Optional[str]:
    """Try each recipe; return name of the one that succeeded, or None."""
    for name, tools, fn in RECIPES + extra:
        missing = [t for t in tools if not have(t) and t != sys.executable]
        if missing:
            info(f"Skipping '{name}' (missing: {', '.join(missing)})")
            continue
        # Cheap pre-check: many recipes bail early if their marker file is absent.
        try:
            info(f"Trying recipe: {name}")
            if fn(src, prefix, jobs):
                return name
        except subprocess.CalledProcessError as e:
            warn(f"Recipe '{name}' failed (exit {e.returncode})")
        except Exception as e:
            warn(f"Recipe '{name}' errored: {e}")
    return None

# ---------------------------------------------------------------------------
# Manual fallback
# ---------------------------------------------------------------------------

def manual_build(src: Path, prefix: Path) -> Optional[Path]:
    """Ask the user for a build command + resulting binary path."""
    print()
    warn("No recipe worked automatically.")
    print(textwrap.dedent(f"""
        Please help me build this project.

        Source directory: {src}
        Install prefix:   {prefix}

        Enter a shell command to build + install (it will run with cwd=source dir).
        Or leave blank to abort.
    """))
    try:
        cmd = input("Build command> ").strip()
    except EOFError:
        return None
    if not cmd:
        return None
    try:
        run(cmd, cwd=src)
    except subprocess.CalledProcessError as e:
        err(f"Manual build failed (exit {e.returncode}).")
        return None

    try:
        path = input("Path to the resulting binary/executable> ").strip()
    except EOFError:
        return None
    if not path:
        return None
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = (src / p).resolve()
    if not p.exists():
        err(f"Path does not exist: {p}")
        return None
    return p

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def default_dest() -> Path:
    """Where the user wants finished binaries."""
    home = Path.home()
    if IS_WINDOWS:
        return home / "Desktop"
    # Try XDG user dirs (Desktop might be localised), else ~/Desktop
    desktop = home / "Desktop"
    return desktop if desktop.exists() else home / "bin"

def find_binary(prefix: Path, name_hint: Optional[str]) -> Optional[Path]:
    """Find the most likely executable under prefix/bin (or prefix itself)."""
    candidates: list[Path] = []
    for d in (prefix / "bin", prefix / "sbin", prefix):
        if d.is_dir():
            for p in d.iterdir():
                if p.is_file() and os.access(p, os.X_OK):
                    if IS_WINDOWS and p.suffix.lower() not in (".exe", ".bat", ".cmd"):
                        continue
                    candidates.append(p)
    if not candidates:
        return None
    if name_hint:
        for c in candidates:
            if c.stem == name_hint or c.name == name_hint:
                return c
    # Prefer exact-name match against archive-derived hint, else first.
    return candidates[0]

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Download, build and install any source project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("source",
                    help="URL, local archive, or already-extracted directory.")
    ap.add_argument("--name", help="Name of the binary to look for / install as.")
    ap.add_argument("--dest", type=Path,
                    help="Where to put the final binary (default: ~/Desktop or ~/bin).")
    ap.add_argument("--prefix", type=Path,
                    help="Install prefix used during build (default: temp dir).")
    ap.add_argument("--jobs", type=int, default=nproc(),
                    help=f"Parallel build jobs (default: {nproc()}).")
    ap.add_argument("--recipes", type=Path,
                    help="Directory of extra *.json recipes.")
    ap.add_argument("--no-guess", action="store_true",
                    help="Skip automatic recipes; go straight to manual mode.")
    ap.add_argument("--keep-tmp", action="store_true",
                    help="Do not delete the temp build directory.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Only show what would be done.")
    args = ap.parse_args(argv)

    dest = args.dest or default_dest()
    dest.mkdir(parents=True, exist_ok=True)
    info(f"Final destination: {dest}")

    if args.dry_run:
        info("Dry run: would download, extract, build, and install.")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="build_any_"))
    info(f"Working directory: {tmp}")
    try:
        src_path_str = args.source
        p = Path(os.path.expanduser(src_path_str))
        if p.exists() and p.is_dir():
            src = p.resolve()
            info(f"Using existing source directory: {src}")
        else:
            archive = download(src_path_str, tmp)
            src = extract(archive, tmp / "src")

        # Derive a name hint from the source dir if not given.
        name_hint = args.name
        if not name_hint:
            # git-2.43.0 -> git ; foo-1.2.3 -> foo
            base = src.name
            for sep in ("-", "_"):
                if sep in base:
                    head = base.split(sep)[0]
                    if head:
                        name_hint = head
                        break
            else:
                name_hint = base
        info(f"Binary name hint: {name_hint}")

        prefix = args.prefix or (tmp / "install")
        prefix.mkdir(parents=True, exist_ok=True)

        chosen: Optional[str] = None
        extra = load_user_recipes(args.recipes) if args.recipes else []
        if not args.no_guess:
            chosen = try_build(src, prefix, args.jobs, extra)

        built_binary: Optional[Path] = None
        if chosen:
            ok(f"Build succeeded using recipe: {chosen}")
            built_binary = find_binary(prefix, name_hint)
        if built_binary is None:
            built_binary = manual_build(src, prefix)

        if built_binary is None:
            err("No binary produced. Aborting.")
            return 1

        final = dest / built_binary.name
        info(f"Installing {built_binary} -> {final}")
        shutil.copy2(built_binary, final)
        try:
            final.chmod(final.stat().st_mode | 0o111)
        except Exception:
            pass
        ok(f"Done. Installed: {final}")
        return 0
    finally:
        if args.keep_tmp:
            info(f"Keeping temp dir: {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        err("Interrupted.")
        sys.exit(130)
