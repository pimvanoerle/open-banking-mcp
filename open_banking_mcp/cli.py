"""Command line entry point: connect banks, inspect and remove connections."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import typer

from .auth import AuthError, TokenManager, authorize
from .cache import Cache
from .client import TrueLayerClient
from .config import ConfigError, load_settings
from .service import DataService
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


def _service(settings, store):
    client = TrueLayerClient(settings, TokenManager(settings, store), psu_ip=settings.psu_ip)
    return DataService(
        client, Cache(settings.cache_file), max_age=timedelta(hours=settings.max_age_hours)
    )


@app.command()
def sync(
    provider_id: str = typer.Argument(None, help="Bank to sync. Defaults to all connected."),
    history_days: int = typer.Option(None, help="How far back to pull. Defaults to config."),
) -> None:
    """Pull bank data into the local cache. Run this daily."""
    settings, store = _load()
    targets = [provider_id] if provider_id else store.providers()
    if not targets:
        typer.echo("No banks connected. Run 'open-banking-mcp auth' first.")
        raise typer.Exit(1)

    service = _service(settings, store)
    failed = False
    for target in targets:
        typer.echo(f"Syncing {target}...")
        try:
            report = asyncio.run(
                service.sync(target, history_days=history_days or settings.history_days)
            )
        except Exception as exc:
            typer.secho(f"  failed: {exc}", fg=typer.colors.RED, err=True)
            failed = True
            continue

        typer.secho(
            f"  {report.accounts} accounts, {report.cards} cards, "
            f"{report.transactions} transactions, {report.pending} pending",
            fg=typer.colors.GREEN if report.ok else typer.colors.YELLOW,
        )
        for err in report.errors:
            typer.secho(f"  ! {err}", fg=typer.colors.YELLOW, err=True)
        failed = failed or not report.ok

    raise typer.Exit(1 if failed else 0)


@app.command()
def cache() -> None:
    """Show what the local cache holds."""
    settings, store = _load()
    stats = Cache(settings.cache_file).stats()
    run = Cache(settings.cache_file).last_run()

    typer.echo(f"Transactions: {stats['transactions']} ({stats['pending']} pending)")
    if stats["earliest"]:
        typer.echo(f"Range:        {stats['earliest'][:10]} to {stats['latest'][:10]}")
    typer.echo(f"Snapshots:    {stats['snapshots']}")
    typer.echo(f"Size:         {stats['db_bytes'] / 1e6:.1f} MB")
    if run:
        colour = typer.colors.GREEN if run["status"] == "ok" else typer.colors.RED
        typer.echo(f"Last sync:    {run['finished_at'] or run['started_at']} ", nl=False)
        typer.secho(run["status"], fg=colour)
    else:
        typer.secho("Last sync:    never", fg=typer.colors.YELLOW)


@app.command()
def serve() -> None:
    """Run the MCP server on stdio (this is what MCP clients launch)."""
    from .server import main as serve_main

    serve_main()


if __name__ == "__main__":
    app()
