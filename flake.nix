{
  description = "Provider-agnostic VLM query flow with bundled agent runtimes";

  inputs = {
    # Python 3.15, Node 26, and the Rust 1.95 codex-rs/rust-toolchain.toml names.
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # bun_graph and its patchelf, for the Claude runtime.
    patchnar = {
      url = "github:Wenri/patchnar";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      patchnar,
    }:
    let
      system = "x86_64-linux";

      # What package.nix needs above nixpkgs (LIEF 1.x, the Claude Agent SDK
      # at claude/vendor.json's tag, Python 3.15's fixes); a consumer applies
      # it to its own package set.
      deps = import ./nix/deps.nix;

      tasksolverFor =
        pkgs:
        pkgs.callPackage ./package.nix {
          src = self;
          patchnar = patchnar.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };
    in
    {
      overlays = {
        inherit deps;
        default = final: _: { tasksolver = tasksolverFor final; };
      };

      packages.${system} =
        let
          pkgs = import nixpkgs {
            inherit system;
            config.allowUnfree = true;
            overlays = [ deps ];
          };
        in
        {
          tasksolver = tasksolverFor pkgs;
          default = self.packages.${system}.tasksolver;
        };
    };
}
