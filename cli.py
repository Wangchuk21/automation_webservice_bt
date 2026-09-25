#!/usr/bin/env python3
import warnings
warnings.filterwarnings("ignore")
import argparse
import sys
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config import settings
from provisioners.cpanel import CPanelProvisioner
from provisioners.directadmin import DirectAdminProvisioner
from notifier import send_customer_welcome_email

console = Console()

def get_provisioner(panel_type: str):
    panel = panel_type.lower().strip()
    if panel in ("cpanel", "whm"):
        return CPanelProvisioner(
            host=settings.CPANEL.host,
            ssh_port=settings.CPANEL.ssh_port,
            ssh_user=settings.CPANEL.ssh_user,
            ssh_password=settings.CPANEL.ssh_password,
            ssh_key_path=settings.CPANEL.ssh_key_path,
            whm_api_token=settings.CPANEL.whm_api_token,
            whm_password=settings.CPANEL.whm_password,
            whm_user=settings.CPANEL.whm_user,
            web_url=settings.CPANEL.web_url,
            sftp_port=settings.CPANEL.sftp_port,
            default_plan=settings.CPANEL.default_plan,
            nameservers=settings.CPANEL.nameservers
        )
    elif panel in ("directadmin", "da"):
        return DirectAdminProvisioner(
            host=settings.DIRECTADMIN.host,
            ssh_port=settings.DIRECTADMIN.ssh_port,
            ssh_user=settings.DIRECTADMIN.ssh_user,
            ssh_password=settings.DIRECTADMIN.ssh_password,
            ssh_key_path=settings.DIRECTADMIN.ssh_key_path,
            api_user=settings.DIRECTADMIN.api_user,
            api_password=settings.DIRECTADMIN.api_password,
            web_url=settings.DIRECTADMIN.web_url,
            sftp_port=settings.DIRECTADMIN.sftp_port,
            default_package=settings.DIRECTADMIN.default_package,
            nameservers=settings.DIRECTADMIN.nameservers
        )
    else:
        console.print(f"[bold red]Error:[/bold red] Unknown panel type '{panel_type}'. Use 'cpanel' or 'directadmin'.")
        sys.exit(1)


def handle_test(args):
    prov = get_provisioner(args.panel)
    console.print(f"\n[bold blue]Testing connection to {args.panel.upper()} server ({prov.host})...[/bold blue]")
    result = prov.test_connection()
    if result.get("success"):
        console.print(f"[bold green]✓ SUCCESS:[/bold green] Connected via {result.get('method')}: {result.get('message')}")
    else:
        console.print(f"[bold red]✗ FAILED:[/bold red] {result.get('message')}")


def handle_create(args):
    prov = get_provisioner(args.panel)
    mode_text = " [yellow](DRY RUN)[/yellow]" if args.dry_run else ""
    console.print(f"\n[bold cyan]⚡ Provisioning user account for [white]{args.domain}[/white] on [yellow]{args.panel.upper()}[/yellow]{mode_text}...[/bold cyan]")
    
    result = prov.create_account(
        domain=args.domain,
        username=args.username,
        password=args.password,
        email=args.email,
        package=args.package,
        dry_run=args.dry_run
    )

    if not result.success:
        console.print(Panel(
            f"[bold red]Provisioning Failed:[/bold red]\n{result.message}",
            title="[red]Error[/red]",
            border_style="red"
        ))
        sys.exit(1)

    # Success Table
    table = Table(title=f"Account Provisioned: {result.domain}", title_style="bold green")
    table.add_column("Property", style="bold cyan")
    table.add_column("Details", style="white")

    table.add_row("Control Panel", result.panel.upper())
    table.add_row("Web UI Login URL", f"[link={result.web_url}]{result.web_url}[/link]")
    table.add_row("Customer Username", result.username)
    table.add_row("Customer Password", f"[bold yellow]{result.password}[/bold yellow]")
    table.add_row("Customer Email", result.email)
    table.add_row("SFTP Host", result.sftp_host)
    table.add_row("SFTP Port", str(result.sftp_port))
    table.add_row("SFTP Document Root", f"[green]{result.doc_root}[/green]")
    table.add_row("DNS Management", "[bold green]Internal / Managed by Support (Customer does not edit)[/bold green]")

    console.print(table)

    # Print copy-pasteable handover box
    console.print(Panel(
        result.handover_text.strip(),
        title="[bold green]Customer Handover Information (Ready to Copy/Send)[/bold green]",
        subtitle="Web UI & SFTP Ready",
        border_style="green"
    ))

    # Save to file if requested
    if args.save:
        filename = f"handover_{result.domain.replace('.', '_')}.txt"
        with open(filename, "w") as f:
            f.write(result.handover_text)
        console.print(f"[dim]Handover details saved to [white]{filename}[/white][/dim]")

    # Email to customer if requested
    if args.email_customer:
        email_ok, email_msg = send_customer_welcome_email(result)
        if email_ok:
            console.print(f"[bold green]✓ Email Sent:[/bold green] {email_msg}")
        else:
            console.print(f"[yellow]! Email Warning:[/yellow] {email_msg}")


def main():
    parser = argparse.ArgumentParser(description="Shared Hosting Account Automation (cPanel & DirectAdmin)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Test command
    test_parser = subparsers.add_parser("test", help="Test connectivity to hosting server")
    test_parser.add_argument("--panel", choices=["cpanel", "directadmin"], required=True, help="Target panel")
    test_parser.set_defaults(func=handle_test)

    # Create command
    create_parser = subparsers.add_parser("create", help="Create new hosting user account")
    create_parser.add_argument("--panel", choices=["cpanel", "directadmin"], required=True, help="Target panel")
    create_parser.add_argument("--domain", required=True, help="Customer domain (e.g., example.bt)")
    create_parser.add_argument("--username", help="Customer username (auto-generated if omitted)")
    create_parser.add_argument("--password", help="Customer password (auto-generated if omitted)")
    create_parser.add_argument("--email", help="Customer contact email")
    create_parser.add_argument("--package", help="Hosting package/plan name (default used if omitted)")
    create_parser.add_argument("--dry-run", action="store_true", help="Simulate creation without connecting to server")
    create_parser.add_argument("--email-customer", action="store_true", help="Send welcome email via SMTP")
    create_parser.add_argument("--save", action="store_true", help="Save handover text to a file")
    create_parser.set_defaults(func=handle_create)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
