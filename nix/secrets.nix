{ pkgs }:

# Only ciphertext and its public key enter the store. dotenvx Armor resolves
# decryption credentials at runtime using the user's existing authentication.
pkgs.writeShellApplication {
  name = "with-secrets";
  runtimeInputs = [ pkgs.dotenvx ];
  text = ''
    if (( $# == 0 )); then
      printf '%s\n' 'Usage: with-secrets COMMAND [ARG...]' >&2
      exit 64
    fi

    env_file="''${DOTENV_FILE:-${../.env}}"
    if [[ ! -f "$env_file" ]]; then
      printf 'Environment file does not exist: %s\n' "$env_file" >&2
      exit 66
    fi

    options=(run --strict --quiet -f "$env_file")

    exec dotenvx "''${options[@]}" -- "$@"
  '';
}
