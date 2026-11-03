# What package.nix needs above nixpkgs 26.05, in one copy: flake.nix exports
# it as overlays.deps, and nix-configs applies it from its `tasksolver` input.
# Drop an entry once nixpkgs carries it.
final: prev:
let
  inherit (final) lib;
  sdk = (lib.importJSON ./hashes.json).claudeAgentSdk;

  # PyO3 releases stop at Python 3.14; its stable-ABI forward compatibility
  # builds an extension that keeps to the limited API on a newer one.
  pyo3Forward =
    pkg:
    pkg.overridePythonAttrs (old: {
      env = (old.env or { }) // {
        PYO3_USE_ABI3_FORWARD_COMPATIBILITY = "1";
      };
    });
  skipTests =
    names: pkg:
    pkg.overridePythonAttrs (old: {
      disabledTests = (old.disabledTests or [ ]) ++ names;
    });
  skipTestPaths =
    paths: pkg:
    pkg.overridePythonAttrs (old: {
      disabledTestPaths = (old.disabledTestPaths or [ ]) ++ paths;
    });

  # LIEF 1.x for the agy conversion (antigravity/symbols/make_libagy.py);
  # nixpkgs has 0.17.0. Only Python 3.15's lief takes it (below): nixpkgs'
  # nodejs links the top-level lief from 25.6 on, so replacing that one
  # would build every nodejs from source.
  lief = prev.lief.overrideAttrs (
    finalAttrs: _: {
      version = "1.0.0";
      src = final.fetchFromGitHub {
        owner = "lief-project";
        repo = "LIEF";
        tag = finalAttrs.version;
        hash = "sha256-Qrk1k/M1fyCDpm0yyVps60mFtrN2oIEjh7REalEPGno=";
      };
    }
  );
in
{
  pythonPackagesExtensions = prev.pythonPackagesExtensions ++ [
    (_: pyprev: {
      # The Claude Agent SDK at claude/vendor.json's tag (nixpkgs has 0.2.82),
      # which claude/vendor/sdk is byte for byte; sniffio and jsonschema have
      # joined its dependencies since.
      claude-agent-sdk = pyprev.claude-agent-sdk.overridePythonAttrs (old: {
        version = lib.removePrefix "v" sdk.tag;
        src = final.fetchFromGitHub {
          owner = "anthropics";
          repo = "claude-agent-sdk-python";
          inherit (sdk) tag hash;
        };
        dependencies = old.dependencies ++ [
          pyprev.sniffio
          pyprev.jsonschema
        ];
        # These suites spawn a stand-in CLI through /usr/bin/env, which the
        # build sandbox lacks, and read the child's state with ps.
        postPatch = (old.postPatch or "") + ''
          substituteInPlace tests/test_close_cancellation.py tests/test_run_end_subprocess.py \
            --replace-fail '#!/usr/bin/env python3' '#!${pyprev.python.interpreter}'
        '';
        nativeCheckInputs = (old.nativeCheckInputs or [ ]) ++ [ final.procps ];
      });
    })

    # Python 3.15 is newer than this package set, built by no Hydra: what
    # TaskSolver's closure needs, for that interpreter only.
    (
      _: pyprev:
      lib.optionalAttrs (pyprev.pythonAtLeast "3.15") {
        # LIEF 1.x's `py` output, as python-packages.nix makes each
        # python3xxPackages.lief from the top-level one.
        lief = (pyprev.toPythonModule (lief.override { python3 = pyprev.python; })).py;
        # TaskSolver's bound (pyproject's build-system, >=0.33,<0.34): the
        # Claude runtime's ELF edit passes Container.update() keywords that
        # 0.32 rejects. 3.15 is TaskSolver's interpreter, so the bump stays here.
        pyelftools = pyprev.pyelftools.overridePythonAttrs (old: rec {
          version = "0.33";
          src = final.fetchFromGitHub {
            owner = "eliben";
            repo = "pyelftools";
            tag = "v${version}";
            hash = "sha256-NqF5A6PRl4uncwNS9j44fIV7ovlljiucvV2Msv0LK/w=";
          };
        });
        jiter = pyo3Forward pyprev.jiter;
        rpds-py = pyo3Forward pyprev.rpds-py;
        # 2.41's core uses CPython API outside the stable ABI, so it needs
        # the release that knows 3.15, now in pydantic's own repository, and
        # pydantic pins its core: both at nixpkgs-unstable's versions.
        pydantic-core = pyprev.pydantic-core.overridePythonAttrs (old: rec {
          version = "2.46.4";
          src = final.fetchFromGitHub {
            owner = "pydantic";
            repo = "pydantic";
            tag = "core-v${version}";
            hash = "sha256-G4Xo6BF6tOn4g/qG3RNDP3/+lYnCOuw3AB1OrVOGcSA=";
          };
          sourceRoot = "${src.name}/pydantic-core";
          cargoDeps = final.rustPlatform.fetchCargoVendor {
            inherit (old) pname;
            inherit version src sourceRoot;
            hash = "sha256-5L317YTV7/Bc/YJLLzc745oJntiYkcZupdeUxiQwcOU=";
          };
        });
        pydantic = pyprev.pydantic.overridePythonAttrs (old: rec {
          version = "2.13.4";
          src = final.fetchFromGitHub {
            owner = "pydantic";
            repo = "pydantic";
            tag = "v${version}";
            hash = "sha256-G4Xo6BF6tOn4g/qG3RNDP3/+lYnCOuw3AB1OrVOGcSA=";
          };
          # The 3.14.1 fix is in this release; pytest 9.1's is not.
          patches = [
            (final.fetchpatch {
              name = "pytest-9.1-compat.patch";
              url = "https://github.com/pydantic/pydantic/commit/f257d0155c6643fbda9516af6b2c4ca082ed7651.patch";
              excludes = [ "uv.lock" ];
              hash = "sha256-azclSDYY/H8RcerNvI07njwLzr8fyIZ17nM18y/edVo=";
            })
          ];
          # The monorepo's core suite as well as its benchmarks.
          disabledTestPaths = (old.disabledTestPaths or [ ]) ++ [ "tests/pydantic_core" ];
          # base64's url-safe decoding changed its leniency in 3.15.
          disabledTests = (old.disabledTests or [ ]) ++ [ "test_base64url" ];
        });
        # pydantic 2.13 made PydanticUserError a RuntimeError (a TypeError
        # before), so a tool whose return type has no JSON schema failed its
        # registration instead of getting no output schema: upstream's fix,
        # in 1.27.1.
        mcp = pyprev.mcp.overridePythonAttrs (old: {
          patches = (old.patches or [ ]) ++ [
            (final.fetchpatch {
              name = "pydantic-2.13-compat.patch";
              url = "https://github.com/modelcontextprotocol/python-sdk/commit/8d4c2f5834f9925b969809d4db415bba130ee4e8.patch";
              hash = "sha256-MMRbeALhQSeiK6HYdpxvtciNKuHS4GVDxh3XFDnBPEE=";
            })
          ];
        });

        # Test suites with cases 3.15 changed under them.
        exceptiongroup = skipTests [ "test_nameerror_suggestions_in_group" ] pyprev.exceptiongroup;
        # test_find_uses_provided_random passes or fails by xdist's test
        # placement: 3.15's linecache warns that a traceback frame's module
        # globals lack a __spec__.loader (an error under the suite's filters)
        # unless that worker already cached the file.
        hypothesis = skipTestPaths [ "tests/cover/test_lookup.py" ] (
          skipTests [
            "test_interactive_example_does_not_emit_warning"
            "test_find_uses_provided_random"
          ] pyprev.hypothesis
        );
        meson-python = skipTests [
          "test_limited_api"
          "test_tag_stable_abi"
        ] pyprev.meson-python;
        mypy = skipTestPaths [ "mypyc/test/test_run.py" ] pyprev.mypy;
        parso = skipTests [ "test_python_exception_matches" ] pyprev.parso;
        pure-eval = skipTestPaths [ "tests/test_getattr_static.py" ] (
          skipTests [
            "test_eval_attrs"
            "test_is_expression_interesting"
          ] pyprev.pure-eval
        );
        setproctitle = skipTests [ "test_clear_segfault" ] pyprev.setproctitle;
        sqlalchemy = skipTests [ "test_date_no_string" ] pyprev.sqlalchemy;
        time-machine = skipTests [ "test_date_today" ] pyprev.time-machine;
        jedi = skipTestPaths [ "test/test_api/test_environment.py" ] (
          skipTests [ "test_venv_and_pths" ] pyprev.jedi
        );
        # Every import-hook case (importlib's cache_from_source changed), and
        # the suites whose fixture imports typing.no_type_check_decorator,
        # removed in 3.15 (only tests/dummymodule.py uses it).
        typeguard = skipTestPaths [
          "tests/test_instrumentation.py"
          "tests/test_pytest_plugin.py"
        ] (skipTests [ "importhook" ] pyprev.typeguard);
        astroid = skipTests [
          "test_typing_object_notsubscriptable_3"
          "test_kw_only_sentinel"
          "test_regression_parse_deeply_nested_parentheses"
        ] pyprev.astroid;
        botocore = skipTestPaths [ "tests/unit/test_compress.py" ] pyprev.botocore;
        rich = skipTestPaths [ "tests/test_inspect.py" ] (skipTests [ "test_attrs_broken" ] pyprev.rich);
        uvloop = skipTests [ "test_socket_sync_remove" ] pyprev.uvloop;
        distutils = skipTests [ "test_get_host_platform" ] pyprev.distutils;
        scikit-build-core = skipTests [ "test_abi3_wheel" ] pyprev.scikit-build-core;
        yarl = skipTests [ "test_pickle" ] pyprev.yarl;
        # Its bytecode-to-AST verifier fails across the sample suite: 3.15
        # changed the bytecode it maps.
        executing = pyprev.executing.overridePythonAttrs { doCheck = false; };
        # Its suite crashes pytest-xdist in the gc tests on 3.15.
        scipy = pyprev.scipy.overridePythonAttrs { doCheck = false; };
        # Its suite needs fastapi, whose test closure does not build for
        # 3.15 (watchfiles' and pendulum's Rust extensions).
        # It also gets starlette, a runtime dependency, only through them.
        sse-starlette = pyprev.sse-starlette.overridePythonAttrs (old: {
          doCheck = false;
          dependencies = old.dependencies ++ [ pyprev.starlette ];
        });
        # Teleportation reads code.co_lnotab, removed in 3.15; pwntools uses
        # rpyc only for its gdb API.
        rpyc = skipTests [ "test_compat" ] pyprev.rpyc;
        # Its suite fails on 3.15's Unicode tables and times out in the
        # debugger tests.
        ipython = pyprev.ipython.overridePythonAttrs { doCheck = false; };
        # Every test module fails at import (importlib's cache_from_source).
        stack-data = pyprev.stack-data.overridePythonAttrs { doCheck = false; };
        # Every case of its one test file errors (its builtins patching).
        blockbuster = pyprev.blockbuster.overridePythonAttrs { doCheck = false; };
        # Its suite runs under blockbuster, whose patching fails the same way:
        # 4,331 errors, 67 passes.
        aiohttp = pyprev.aiohttp.overridePythonAttrs { doCheck = false; };
        # Django is here only for pytest-randomly's test inputs (factory-boy,
        # model-bakery, pytest-django). Seven of its 18,166 cases fail on
        # 3.15: argparse's new "maybe you meant", strip_tags' table of
        # versions, and five template tags with an annotation naming an
        # undefined type (a NameError at registration).
        django = pyprev.django.overridePythonAttrs { doCheck = false; };
        # django-configurations' importer has only load_module(), and 3.15
        # removed importlib's fallback to it, so its settings do not import;
        # nothing here uses it beyond this suite.
        pytest-django = skipTestPaths [ "tests/test_django_configurations.py" ] pyprev.pytest-django;
        # Their suites need pendulum, whose Rust extension uses CPython API
        # outside the stable ABI and so does not compile for 3.15; nothing
        # needs pendulum at run time.
        pydantic-extra-types = pyprev.pydantic-extra-types.overridePythonAttrs { doCheck = false; };
        sqlalchemy-utils = pyprev.sqlalchemy-utils.overridePythonAttrs { doCheck = false; };
        # A case in this file aborts the interpreter.
        zlib-ng = skipTestPaths [ "tests/test_zlib_compliance.py" ] pyprev.zlib-ng;
        # unittestCheckHook ignores disabledTests: take the case out of
        # discovery instead.
        websockets = pyprev.websockets.overridePythonAttrs (old: {
          preCheck = (old.preCheck or "") + ''
            substituteInPlace tests/test_utils.py --replace-fail \
              'def test_apply_mask_non_contiguous_memoryview' 'def _apply_mask_non_contiguous_memoryview'
          '';
        });
        # The module builds; the GUI suites' DefaultRootTest cases fail under
        # the build's Xvfb ("module 'tkinter' has no attribute '_default_root'").
        tkinter = pyprev.tkinter.overridePythonAttrs { doCheck = false; };
      }
    )
  ];
}
