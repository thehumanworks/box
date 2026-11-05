{ pkgs }:

# Only file paths and command arguments enter this declaration. Secret values
# are decrypted by dotenvx at runtime, never by the Nix evaluator or builder.
pkgs.writeShellApplication {
  name = "with-secrets";
  runtimeInputs = [ pkgs.dotenvx ];
  text = ''
    if (( $# == 0 )); then
      printf '%s\n' 'Usage: with-secrets COMMAND [ARG...]' >&2
      exit 64
    fi

    env_file="''${DOTENV_FILE:-.env}"
    if [[ ! -f "$env_file" ]]; then
      printf 'Environment file does not exist: %s\n' "$env_file" >&2
      exit 66
    fi

    options=(run --strict --quiet -f "$env_file")
    if [[ -n "''${DOTENV_KEY_FILE:-}" ]]; then
      if [[ ! -f "$DOTENV_KEY_FILE" ]]; then
        printf 'Private-key file does not exist: %s\n' "$DOTENV_KEY_FILE" >&2
        exit 66
      fi
      options+=(-fk "$DOTENV_KEY_FILE")
    fi

    exec dotenvx "''${options[@]}" -- "$@"
  '';
}
