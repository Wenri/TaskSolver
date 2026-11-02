# TaskSolver with its bundled native runtimes, built as setup.py builds them,
# from pinned inputs only. The pins are this tree's own, read at evaluation:
# antigravity/vendor.json (agy), claude/vendor.json (Claude Code and the SDK
# tag), the vendored codex-rs (with upstream's codex-rs/default.nix) and Kimi
# Code's lockfile. The hashes those cannot carry are nix/hashes.json's, which
# nix/update-hashes recomputes from the tree after a bump.
#
# A caller may substitute a runtime input with its own build of the same pin
# (nix-configs passes its recipes):
#   agyArchive        the pinned agy release tarball
#   claudeExecutable  the pinned official `claude` executable
#   codexBase         a Codex build of the vendored version to layer the
#                     wirecap link on (default: upstream's codex-rs/default.nix
#                     with the rusty_v8 assets below)
# `patchnar` supplies bun_graph and the patchelf that keeps Bun's version
# tables. `python315` must carry lief 1.x and claude-agent-sdk at
# claude/vendor.json's tag; flake.nix's `deps` overlay adds both to nixpkgs.
#
# The natives find python3-config, libpython, Boost.Python and the N-API
# headers under $CONDA_PREFIX, so `prefix` is that environment, and their
# rpaths keep it in the closure. The Claude runtime is the SDK's
# claude_agent_sdk/_bundled, where pyclaude looks for it, so the SDK comes
# from python315 with the runtime added and the vendored copy stays out of the
# wheel. agy and Claude Code are proprietary, hence `unfree`.
{
  lib,
  stdenv,
  fetchurl,
  symlinkJoin,
  runCommand,
  callPackage,
  rustPlatform,
  python315,
  nodejs_26,
  pnpm_11,
  fetchPnpmDeps,
  pnpmConfigHook,
  cmake,
  ninja,
  pkg-config,
  linuxHeaders,
  libclang,
  src,
  patchnar,
  agyArchive ? null,
  claudeExecutable ? null,
  codexBase ? null,
}:
let
  readJSON = path: builtins.fromJSON (builtins.readFile "${src}/${path}");
  inherit ((builtins.fromTOML (builtins.readFile "${src}/pyproject.toml")).project) version;
  agyPin = (readJSON "antigravity/vendor.json").cli;
  claudePins = readJSON "claude/vendor.json";
  sdkVersion = lib.removePrefix "v" claudePins.sdk.tag;

  # Content-addressed copies of the subtrees the runtimes build from, so a
  # change elsewhere in the tree (docs, a version bump) leaves them built.
  subtree =
    dir:
    builtins.path {
      name = baseNameOf dir;
      path = "${src}/${dir}";
    };
  codexRs = subtree "codex/vendor/codex-rs";
  codexVersion =
    (builtins.fromTOML (builtins.readFile "${codexRs}/Cargo.toml")).workspace.package.version;
  # The hashes the pins cannot carry, kept by nix/update-hashes: the crates
  # of codex-rs/Cargo.lock, Kimi Code's pnpm store, and denoland/rusty_v8's
  # release assets for the `v8` crate that lockfile names (for the default
  # codex base).
  hashes = readJSON "nix/hashes.json";
  inherit (hashes) rustyV8;

  agyArchive' =
    if agyArchive != null then
      agyArchive
    else
      fetchurl {
        inherit (agyPin) url;
        sha512 = agyPin.archive_sha512;
      };
  claudeExecutable' =
    if claudeExecutable != null then
      claudeExecutable
    else
      fetchurl {
        inherit (claudePins.cli) url sha256;
      };

  # The conda environment the CMake and Cargo builds expect: python3 and
  # python3-config, libpython, Boost.Python and Thread for this Python, the
  # N-API headers, and the kernel-headers sysroot antigravity/CMakeLists.txt
  # requires when the build has no /usr/include.
  python = python315;
  boost = python.pkgs.boost;
  sysroot = runCommand "tasksolver-uapi-sysroot" { } ''
    mkdir -p $out/x86_64-conda-linux-gnu/sysroot/usr
    ln -s ${linuxHeaders}/include $out/x86_64-conda-linux-gnu/sysroot/usr/include
  '';
  prefix = symlinkJoin {
    name = "tasksolver-prefix";
    paths = [
      python
      boost.out
      boost.dev
      nodejs_26
      sysroot
    ];
  };

  # The static bridge Codex links (setup.py's standalone wirecap/native build).
  bridge = stdenv.mkDerivation {
    # Unversioned: Codex links it, so TaskSolver's releases must not move it.
    name = "tasksolver-wirecap-bridge";
    src = subtree "wirecap/native";
    nativeBuildInputs = [
      cmake
      ninja
    ];
    env.CONDA_PREFIX = prefix;
    installPhase = ''
      runHook preInstall
      install -Dm644 libwirecap_bridge.a $out/lib/libwirecap_bridge.a
      runHook postInstall
    '';
  };

  defaultCodexBase = (callPackage codexRs { version = codexVersion; }).overrideAttrs (old: {
    env = old.env // {
      LIBCLANG_PATH = "${lib.getLib libclang}/lib";
      # GCC's stringop-overflow false positive in BoringSSL's a_bitstr.cc.
      NIX_CFLAGS_COMPILE = "-Wno-error=stringop-overflow";
      RUSTY_V8_ARCHIVE = fetchurl {
        name = "librusty_v8-${rustyV8.version}";
        url = "https://github.com/denoland/rusty_v8/releases/download/v${rustyV8.version}/librusty_v8_release_x86_64-unknown-linux-gnu.a.gz";
        hash = rustyV8.archive;
      };
      RUSTY_V8_SRC_BINDING_PATH = fetchurl {
        name = "src_binding-${rustyV8.version}";
        url = "https://github.com/denoland/rusty_v8/releases/download/v${rustyV8.version}/src_binding_release_x86_64-unknown-linux-gnu.rs";
        hash = rustyV8.binding;
      };
    };
  });
  # The vendored, patched CLI only: pycodex runs the binary itself, so no
  # wrapper, completions or code-mode host (a base's install hooks for those
  # go too).
  codex = (if codexBase != null then codexBase else defaultCodexBase).overrideAttrs (old: {
    pname = "tasksolver-codex";
    src = codexRs;
    cargoDeps = rustPlatform.fetchCargoVendor {
      src = codexRs;
      hash = hashes.codexCargo.hash;
    };
    cargoBuildFlags = [
      "--bin"
      "codex"
    ];
    env = old.env // {
      WIRECAP_BRIDGE_DIR = "${bridge}/lib";
      CONDA_PREFIX = prefix;
    };
    postInstall = "";
    postFixup = "";
    postInstallCheck = "";
  });

  # The cleaned Bun runs during staging (bytecode caches) before its hash is
  # taken: give it the store's loader first.
  claudeRuntime = stdenv.mkDerivation {
    pname = "tasksolver-claude-runtime";
    inherit (claudePins.cli) version;
    # `claude/` itself, unpacked beside the build so `claude.build_cli` runs
    # as a package.
    src = subtree "claude";
    sourceRoot = ".";
    nativeBuildInputs = [
      (python.withPackages (ps: [
        ps.pyelftools
        ps.pwntools
      ]))
      patchnar
    ];
    postPatch = ''
      substituteInPlace claude/unbundle.py \
        --replace-fail \
          '        candidate, native_patches = _compile_helpers(output, staging)' \
          '        candidate, native_patches = _compile_helpers(output, staging)
              subprocess.run([os.environ["PATCHELF"], "--set-interpreter",
                              os.environ["TASKSOLVER_BUN_INTERPRETER"], str(bun)], check=True)'
    '';
    env = {
      # node_api.h for the libwire.node addon the staging compiles.
      CONDA_PREFIX = prefix;
      # The runtime has no lib/: stdenv's -rpath $out/lib would only name
      # this path in libwire.node, keeping it beside claudeSdk's copy.
      NIX_NO_SELF_RPATH = "1";
      PATCHELF = "${patchnar}/bin/patchelf";
      TASKSOLVER_BUN_INTERPRETER = stdenv.cc.bintools.dynamicLinker;
    };
    buildPhase = ''
      runHook preBuild
      export HOME=$TMPDIR
      install -Dm755 ${claudeExecutable'} claude/build/cache/claude-${claudePins.cli.sha256}
      echo "${claudePins.cli.sha256}  claude/build/cache/claude-${claudePins.cli.sha256}" | sha256sum -c
      python3 -m claude.build_cli
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      cp -r claude/vendor/sdk/src/claude_agent_sdk/_bundled $out
      runHook postInstall
    '';
    # The manifest hashes every file as staged.
    dontFixup = true;
  };
  # After the fixup, which would otherwise strip and patch the runtime.
  claudeSdk = python.pkgs.claude-agent-sdk.overridePythonAttrs (old: {
    postFixup = (old.postFixup or "") + ''
      if [ "${old.version}" != "${sdkVersion}" ]; then
        echo "tasksolver: claude/vendor.json pins claude-agent-sdk ${sdkVersion}, python315 has ${old.version}" >&2
        exit 1
      fi
      # Into the directory itself: the SDK ships a .gitignore there, so
      # copying the runtime as a whole would nest it one level down.
      cp -r ${claudeRuntime}/. $out/${python.sitePackages}/claude_agent_sdk/_bundled/
    '';
  });

  pnpm = pnpm_11.override { nodejs = nodejs_26; };
  kimiRoot = "kimi/vendor/kimi-code";
in
python.pkgs.buildPythonPackage {
  pname = "tasksolver";
  inherit version src;
  pyproject = true;

  postPatch = ''
    # Nixpkgs' pnpm instead of `npx -y pnpm@<pin>`, offline over the
    # pnpmConfigHook store; Codex is the pre-placed `codex`, and the Claude
    # runtime ships with claudeSdk.
    substituteInPlace setup.py \
      --replace-fail 'pnpm = ["npx", "-y", pin]' 'pnpm = ["pnpm"]' \
      --replace-fail '"install", "--frozen-lockfile",' '"install", "--offline", "--frozen-lockfile",' \
      --replace-fail '["cargo", "build", "--release", "-p", "codex-cli"]' '["true"]' \
      --replace-fail 'self._build_claude(root, env)' 'pass'
    substituteInPlace pyproject.toml \
      --replace-fail ', "claude/vendor/sdk/src"]' ']' \
      --replace-fail ', "claude_agent_sdk*"]' ']'
  '';

  build-system = with python.pkgs; [
    setuptools
    lief
    pyelftools
    capstone
    pwntools
  ];
  dependencies =
    (with python.pkgs; [
      loguru
      pillow
      numpy
      requests
      tqdm
      shortuuid
      pymongo
      openai
      anthropic
      anyio
      sniffio
      mcp
      jsonschema
      typing-extensions
      pydantic
      packaging
      google-generativeai
      pygit2
    ])
    ++ [ claudeSdk ];
  # The bound is the Codex SDK's, above nixpkgs 26.05's packaging.
  pythonRelaxDeps = [ "packaging" ];

  nativeBuildInputs = [
    cmake
    ninja
    pkg-config
    nodejs_26
    pnpm
    (pnpmConfigHook.override { inherit pnpm; })
  ];
  # setup.py runs CMake and Ninja itself, in three trees.
  dontUseCmakeConfigure = true;
  dontUseNinjaBuild = true;
  dontUseNinjaInstall = true;
  dontUseNinjaCheck = true;

  pnpmRoot = kimiRoot;
  pnpmDeps = fetchPnpmDeps {
    pname = "tasksolver-kimi-code";
    inherit pnpm;
    inherit ((lib.importJSON "${subtree kimiRoot}/apps/kimi-code/package.json")) version;
    src = subtree kimiRoot;
    fetcherVersion = 3;
    hash = hashes.kimiPnpm.hash;
  };

  env = {
    CONDA_PREFIX = prefix;
    # pnpm would otherwise fetch the packageManager field's exact version.
    npm_config_manage_package_manager_versions = "false";
  };

  preBuild = ''
    export HOME=$TMPDIR
    # Where antigravity/CMakeLists.txt caches the download it verified.
    mkdir -p antigravity/build
    tar -xzf ${agyArchive'} -C antigravity/build antigravity
    echo "${agyPin.sha512}  antigravity/build/antigravity" | sha512sum -c
    mv antigravity/build/antigravity antigravity/build/antigravity-${agyPin.archive_sha512}.raw
    install -Dm755 ${codex}/bin/codex codex/vendor/codex-rs/target/release/codex
  '';

  # libagy.so's build-id is bound to symbols.json, and the natives' rpaths
  # name the prefix: no strip, no rpath shrinking.
  dontStrip = true;
  dontPatchELF = true;

  pythonImportsCheck = [ "tasksolver" ];
  # pyclaude requires the bundled runtime where the SDK keeps it.
  postInstallCheck = ''
    PYTHONPATH=$out/${python.sitePackages}:$PYTHONPATH ${python.interpreter} -c \
      'from pyclaude._env import bundled_cli; print(bundled_cli())'
  '';

  passthru = {
    inherit
      bridge
      codex
      prefix
      claudeRuntime
      claudeSdk
      ;
    agyArchive = agyArchive';
    claudeExecutable = claudeExecutable';
  };

  meta = {
    description = "Provider-agnostic VLM query flow with API, agent SDK, linked CLI and local backends";
    homepage = "https://github.com/Wenri/TaskSolver";
    license = with lib.licenses; [
      gpl3Only
      unfree
    ];
    platforms = [ "x86_64-linux" ];
  };
}
