"""Connect the prepared OpenPGP signer to Git configuration."""

from ..gpg import configuration


def signing_options(signing: configuration.GpgSigning) -> tuple[str, ...]:
    return (
        "-c",
        "gpg.format=openpgp",
        "-c",
        f"gpg.openpgp.program={signing.wrapper}",
        "-c",
        f"user.signingKey={signing.fingerprint}",
        "-c",
        "commit.gpgSign=true",
    )
