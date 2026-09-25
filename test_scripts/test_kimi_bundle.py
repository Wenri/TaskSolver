#!/usr/bin/env python3
"""Offline smoke test for the built Kimi artifacts staged inside a Python package.

No model, credentials, or network: the CLI only reports its version, and its search
worker opens an empty temporary index. Running outside the checkout catches bare
npm imports and missing sibling workers that source-tree runs hide. Skips when the
native build artifacts or Node runtime are absent.

    python3 test_scripts/test_kimi_bundle.py
"""

import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
from typing import Final
from unittest.mock import patch

ROOT: Final = Path(__file__).resolve().parents[1]
NODE: Final = shutil.which("node")
DIST: Final = ROOT / "kimi/vendor/kimi-code/apps/kimi-code/dist"
ARTIFACTS: Final = {
    "main.mjs": DIST / "main.mjs",
    "search-worker.mjs": DIST / "search-worker.mjs",
    "wirecap_node.node": ROOT / "kimi/native/build/wirecap_node.node",
}


def main():
    missing: Final = [str(path) for path in ARTIFACTS.values() if not path.is_file()]
    if NODE is None or len(missing) == len(ARTIFACTS):
        print(
            "SKIP: build Kimi artifacts and install Node before running this smoke test"
        )
        sys.exit(0)
    assert not missing, f"Incomplete Kimi build: {missing}"
    expected_version: Final = json.loads((DIST.parent / "package.json").read_text())[
        "version"
    ]
    with patch("setuptools.setup"):
        setup: Final = runpy.run_path(ROOT / "setup.py")
    with tempfile.TemporaryDirectory(prefix="tasksolver-kimi-package-") as temp:
        sandbox: Final = Path(temp)
        fake_root: Final = sandbox / "source"
        fake_root.mkdir()
        (fake_root / "kimi").symlink_to(ROOT / "kimi", target_is_directory=True)
        stage: Final = sandbox / "wheel"
        stage.mkdir()
        shutil.copytree(
            ROOT / "kimi/pykimi",
            stage / "pykimi",
            ignore=shutil.ignore_patterns("__pycache__", "vendor"),
        )
        shutil.copytree(
            ROOT / "wirecap",
            stage / "wirecap",
            ignore=shutil.ignore_patterns("__pycache__", "native"),
        )
        builder: Final = setup["BuildPyNative"](setup["BinaryDistribution"]())
        builder.build_lib = str(stage)
        builder._bundle_artifacts(str(fake_root))
        bundle_dir: Final = stage / "pykimi/vendor"
        for name, src in ARTIFACTS.items():
            bundled = bundle_dir / name
            assert bundled.is_file(), f"{name} absent from package"
            assert (
                hashlib.sha256(bundled.read_bytes()).digest()
                == hashlib.sha256(src.read_bytes()).digest()
            ), name
            print(f"PASS: staged {name} matches built artifact", flush=True)
        env: Final = dict(os.environ, PYTHONPATH=str(stage))
        resolved: Final = subprocess.check_output(
            [
                sys.executable,
                "-S",
                "-c",
                "import json; from pykimi._env import KIMI_MAIN, WIRE_NODE_ADDON; print(json.dumps([KIMI_MAIN, WIRE_NODE_ADDON]))",
            ],
            cwd=sandbox,
            env=env,
            text=True,
        )
        assert json.loads(resolved) == [
            str(bundle_dir / "main.mjs"),
            str(bundle_dir / "wirecap_node.node"),
        ]
        print(
            "PASS: Python runtime resolves the package artifacts outside the source tree",
            flush=True,
        )
        clean_env: Final = {
            k: v
            for k, v in env.items()
            if not k.startswith(
                ("WIRE_", "KIMI_MODEL_", "MOONSHOT_", "OPENAI_", "ANTHROPIC_")
            )
        }
        clean_env["KIMI_CODE_HOME"] = str(sandbox / "kimi-home")
        clean_env["KIMI_CODE_NO_AUTO_UPDATE"] = "1"
        version: Final = subprocess.run(
            [str(NODE), str(bundle_dir / "main.mjs"), "--version"],
            cwd=sandbox,
            env=clean_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
        )
        version_ok: Final = (
            version.returncode == 0 and expected_version in version.stdout
        )
        print(
            (
                f"PASS: staged main.mjs --version reports {expected_version}"
                if version_ok
                else "FAIL: staged main version: " + version.stdout
            ),
            flush=True,
        )
        script: Final = sandbox / "worker-smoke.mjs"
        script.write_text("""
    import assert from 'node:assert/strict';
    import { Worker } from 'node:worker_threads';
    import { once } from 'node:events';
    import { pathToFileURL } from 'node:url';
    const [entry, dir] = process.argv.slice(2);
    const worker = new Worker(pathToFileURL(entry), { workerData: { dir, bootSalt: 'offline-package-validation' } });
    const deadline = setTimeout(() => { console.error('worker timeout'); process.exit(1); }, 20000);
    let phase = 0;
    worker.on('error', (err) => { console.error(err); process.exit(1); });
    worker.on('message', (event) => {
      if (event.type === 'error') { console.error(event); process.exit(1); }
      if (event.type === 'ready') {
        assert.equal(event.v, 1);
        console.log('PASS: staged search worker starts without source packages');
        phase = 1;
        worker.postMessage({ id: 1, v: 1, type: 'open' });
      } else if (event.type === 'result' && event.id === 1) {
        assert.equal(event.result.readOnly, false);
        console.log('PASS: staged search worker opens its index');
        phase = 2;
        worker.postMessage({ id: 2, v: 1, type: 'status' });
      } else if (event.type === 'result' && event.id === 2) {
        assert.equal(event.result.documents, 0);
        assert.equal(event.result.sessions, 0);
        console.log('PASS: staged search worker returns empty-index status');
        phase = 3;
        worker.postMessage({ id: 3, v: 1, type: 'close' });
      } else if (event.type === 'result' && event.id === 3) {
        assert.equal(event.result, null);
        phase = 4;
      }
    });
    const [code] = await once(worker, 'exit');
    clearTimeout(deadline);
    assert.equal(code, 0);
    assert.equal(phase, 4);
    console.log('PASS: staged search worker closes cleanly');
    """)
        worker: Final = subprocess.run(
            [
                str(NODE),
                str(script),
                str(bundle_dir / "search-worker.mjs"),
                str(sandbox / "index"),
            ],
            cwd=sandbox,
            env=clean_env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=25,
        )
        print(worker.stdout, end="", flush=True)
        assert worker.returncode == 0
        assert version_ok
    print("PASS: 9 offline package-artifact checks")


if __name__ == "__main__":
    main()
