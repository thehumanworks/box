# The nixpkgs settings that every output of this flake uses.
{ lib }:
{
  # nixpkgs refuses unfree packages. Only the packages named here are permitted.
  allowUnfreePredicate =
    pkg:
    builtins.elem (lib.getName pkg) [
      "claude-code"
    ];
}
