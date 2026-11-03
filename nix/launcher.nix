# TaskSolver's commands, run from a Python environment of their own (package.nix's
# passthru.launcher; flake.nix's default package, so `nix run` opens the terminal):
#   tasksolver BACKEND... [--web-port PORT]   python -m tasksolver.terminal
#   tasksolver-python [ARGS...]                python3 with TaskSolver importable
# The runtimes' embedded interpreters take that environment as PYTHONHOME (wirecap's
# python_home), and package.nix pins nixpkgs' tmux, ttyd and Node by store path, so the
# commands change neither PATH nor anything else in the caller's environment.
{
  runCommand,
  makeWrapper,
  python,
  tasksolver,
}:
let
  env = python.withPackages (_: [ tasksolver ]);
in
runCommand "tasksolver-launcher-${tasksolver.version}"
  {
    nativeBuildInputs = [ makeWrapper ];
    passthru = { inherit env; };
    meta = {
      inherit (tasksolver.meta) homepage license platforms;
      description = "TaskSolver's terminal screens for agy, Codex, Claude Code and Kimi Code, and its Python";
      mainProgram = "tasksolver";
    };
  }
  ''
    makeWrapper ${env}/bin/python3 $out/bin/tasksolver --add-flags "-m tasksolver.terminal"
    makeWrapper ${env}/bin/python3 $out/bin/tasksolver-python
  ''
