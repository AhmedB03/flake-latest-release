{
  description = "Check or pin flake inputs against each repo's latest stable GitHub release";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        rec {
          flake-latest-release = pkgs.python3Packages.buildPythonApplication {
            pname = "flake-latest-release";
            version = "0.1.0";
            pyproject = true;
            src = self;
            build-system = [ pkgs.python3Packages.setuptools ];
            nativeCheckInputs = [ pkgs.python3Packages.pytestCheckHook ];
            meta = {
              description = "Check or pin flake inputs against each repo's latest stable GitHub release";
              mainProgram = "flake-latest-release";
            };
          };
          default = flake-latest-release;
        }
      );
    };
}
