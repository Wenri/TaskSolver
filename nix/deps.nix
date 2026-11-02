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
in
{
  # LIEF 1.x for the agy conversion (antigravity/symbols/make_libagy.py);
  # nixpkgs has 0.17.0. Each python3xxPackages.lief is this package's `py`
  # output for that interpreter, so it follows.
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
      });
    })

    # Python 3.15 is newer than this package set, built by no Hydra: what
    # TaskSolver's closure needs, for that interpreter only.
    (
      _: pyprev:
      lib.optionalAttrs (pyprev.pythonAtLeast "3.15") {
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
        });

        # Test suites with cases 3.15 changed under them.
        exceptiongroup = skipTests [ "test_nameerror_suggestions_in_group" ] pyprev.exceptiongroup;
        hypothesis = skipTestPaths [ "tests/cover/test_lookup.py" ] (
          skipTests [ "test_interactive_example_does_not_emit_warning" ] pyprev.hypothesis
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
        # Every import-hook case: importlib's cache_from_source changed.
        typeguard = skipTests [ "importhook" ] pyprev.typeguard;
        astroid = skipTests [
          "test_typing_object_notsubscriptable_3"
          "test_kw_only_sentinel"
          "test_regression_parse_deeply_nested_parentheses"
        ] pyprev.astroid;
        botocore = skipTestPaths [ "tests/unit/test_compress.py" ] pyprev.botocore;
        rich = skipTestPaths [ "tests/test_inspect.py" ] (skipTests [ "test_attrs_broken" ] pyprev.rich);
        scipy = skipTests [ "test_funcs and cdf and Normal" ] pyprev.scipy;
        uvloop = skipTests [ "test_socket_sync_remove" ] pyprev.uvloop;
        # Every case of its one test file errors (its builtins patching).
        blockbuster = pyprev.blockbuster.overridePythonAttrs { doCheck = false; };
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
