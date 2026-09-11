"""Command line entry point: connect banks, inspect and remove connections."""

from __future__ import annotations

import asyncio

import typer

from .auth import AuthError, TokenManager, authorize
from .config import ConfigError, load_settings
from .storage import KeyringTokenStore, build_store

app = typer.Typer(
    help="Connect UK bank accounts for the Open Banking MCP server.",
    no_args_is_help=True,
    add_completion=False,
)


def _load():
    try:
        settings = load_settings()
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    return settings, build_store(settings)


@app.command()
def auth(
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the auth URL instead of opening a browser."
    ),
    no_pkce: bool = typer.Option(
        False, "--no-pkce", help="Disable PKCE (troubleshooting only)."
    ),
    timeout: float = typer.Option(300.0, help="Seconds to wait for the bank redirect."),
) -> None:
    """Connect a bank account. Opens a browser for consent."""
    settings, store = _load()
    typer.echo(f"Environment: {settings.env}")
    try:
        token = asyncio.run(
            authorize(
                settings,
                store,
                use_pkce=not no_pkce,
                timeout=timeout,
                open_browser=not no_browser,
            )
        )
    except AuthError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    backend = "keychain" if isinstance(store, KeyringTokenStore) else "file"
    typer.secho(f"\nConnected: {token.provider_id}", fg=typer.colors.GREEN)
    typer.echo(f"Scopes:    {token.scope or '(none reported)'}")
    typer.echo(f"Stored in: {backend}")
    typer.echo(
        f"Consent valid until {token.consent_expires_at:%Y-%m-%d} "
        f"({token.consent_days_left} days)."
    )


@app.command()
def status() -> None:
    """Show connected banks and how long their consent has left."""
    settings, store = _load()
    rows = TokenManager(settings, store).consent_status()
    if not rows:
        typer.echo("No banks connected. Run 'open-banking-mcp auth' to add one.")
        raise typer.Exit(0)

    backend = "keychain" if isinstance(store, KeyringTokenStore) else "file"
    typer.echo(f"Environment: {settings.env}   Storage: {backend}\n")
    for provider_id, days_left, expires_at in rows:
        colour = (
            typer.colors.RED
            if days_left <= 7
            else typer.colors.YELLOW
            if days_left <= 21
            else typer.colors.GREEN
        )
        typer.echo(f"  {provider_id:<24} ", nl=False)
        typer.secho(
            f"{days_left:>3} days left (until {expires_at:%Y-%m-%d})", fg=colour
        )


@app.command()
def logout(
    provider_id: str = typer.Argument(..., help="Provider id, e.g. uk-ob-monzo."),
) -> None:
    """Remove a saved bank connection."""
    _, store = _load()
    if provider_id not in store.providers():
        typer.secho(f"No saved connection for {provider_id!r}.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)
    store.delete(provider_id)
    typer.secho(f"Removed {provider_id}.", fg=typer.colors.GREEN)


if __name__ == "__main__":
    app()
