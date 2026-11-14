# ffp: FFP-NDIMMORWYN-FLAKE declarative nix
{
  description = "Ndimmôrwyn v8.7.0 — Tier-3 Autonomous Execution Organism & Constructal Sine-Meander 4-Tier Inference Router";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    tianxia-core = {
      url = "github:Tianxia-Core/TianxiaOS";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs = { self, nixpkgs, tianxia-core, ... }:
    let
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs supportedSystems;
    in {
      packages = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.rustPlatform.buildRustPackage {
            pname = "ndimmorwyn";
            version = "8.7.0";
            src = ./.;
            cargoLock.lockFile = ./Cargo.lock;
          };
        }
      );

      devShells = forAllSystems (system:
        let
          pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            buildInputs = with pkgs; [ rustc cargo python3 sqlite ];
          };
        }
      );
    };
}
