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
from provisioners.base import (
    validate_domain,
    validate_username,
    validate_email,
    validate_package,
    ValidationError,
)
from notifier import send_customer_welcome_email, test_smtp_connection
from nic_client import NICClient

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
    # Validate before any SSH connection is opened, so malformed input can never
    # reach a remote shell. The CLI bypasses the API's pydantic validators.
    try:
        domain = validate_domain(args.domain)
        username = validate_username(args.username) if args.username else None
        email = validate_email(args.email) if args.email else None
        package = validate_package(args.package) if args.package else None
    except ValidationError as e:
        console.print(Panel(
            f"[bold red]Invalid input:[/bold red]\n{e}",
            title="[red]Validation Error[/red]",
            border_style="red"
        ))
        raise SystemExit(1)

    prov = get_provisioner(args.panel)
    mode_text = " [yellow](DRY RUN)[/yellow]" if args.dry_run else ""
    console.print(f"\n[bold cyan]⚡ Provisioning user account for [white]{domain}[/white] on [yellow]{args.panel.upper()}[/yellow]{mode_text}...[/bold cyan]")
    
    result = prov.create_account(
        domain=domain,
        username=username,
        password=args.password,
        email=email,
        package=package,
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

    # Register / update domain on nic.bt.bt if requested
    if getattr(args, "register_nic", False):
        if not args.dry_run:
            console.print(f"\n[bold blue]Updating WHOIS record on nic.bt.bt for {result.domain}...[/bold blue]")
            nic = NICClient()
            nic_res = nic.register_or_update_domain(
                domain=result.domain,
                customer_name=args.customer_name or result.username,
                email=result.email or settings.SMTP_FROM_EMAIL
            )
            if nic_res.get("success"):
                console.print(f"[bold green]✓ NIC WHOIS Updated:[/bold green] {nic_res.get('message')}")
            else:
                console.print(f"[yellow]! NIC WHOIS Warning:[/yellow] {nic_res.get('message')}")
        else:
            console.print(f"[yellow](DRY RUN) Skipped nic.bt.bt WHOIS registration[/yellow]")


def handle_test_smtp(args):
    console.print(f"\n[bold blue]Testing SMTP server ({settings.SMTP_HOST}:{settings.SMTP_PORT}, SSL={settings.SMTP_SSL})...[/bold blue]")
    ok, msg = test_smtp_connection(recipient=args.recipient)
    if ok:
        console.print(f"[bold green]✓ SUCCESS:[/bold green] {msg}")
    else:
        console.print(f"[bold red]✗ FAILED:[/bold red] {msg}")


def handle_test_nic(args):
    console.print(f"\n[bold blue]Testing connection & login to nic.bt.bt ({settings.NIC_URL})...[/bold blue]")
    nic = NICClient()
    ok, msg = nic.login()
    if ok:
        console.print(f"[bold green]✓ SUCCESS:[/bold green] {msg}")
    else:
        console.print(f"[bold red]✗ FAILED:[/bold red] {msg}")


def handle_whois(args):
    console.print(f"\n[bold blue]Querying WHOIS for [white]{args.domain}[/white] on nic.bt.bt...[/bold blue]")
    nic = NICClient()
    res = nic.query_whois(args.domain)
    if res.get("found"):
        console.print(f"[bold green]✓ Domain Found in WHOIS:[/bold green]")
        table = Table(title=f"WHOIS Record: {args.domain}")
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="white")
        for k, v in res.get("data", {}).items():
            table.add_row(k, v)
        console.print(table)
    else:
        console.print(f"[yellow]! {res.get('message')}[/yellow]")


def handle_register_domain(args):
    console.print(f"\n[bold cyan]Submitting domain to nic.bt.bt: [white]{args.domain}[/white]...[/bold cyan]")
    nic = NICClient()
    res = nic.register_or_update_domain(
        domain=args.domain,
        customer_name=args.name,
        email=args.email,
        phone=args.phone or "+975",
        address=args.address or "Thimphu, Bhutan"
    )
    if res.get("success"):
        console.print(f"[bold green]✓ SUCCESS:[/bold green] {res.get('message')}")
    else:
        console.print(f"[bold red]✗ FAILED:[/bold red] {res.get('message')}")


def main():
    parser = argparse.ArgumentParser(description="Shared Hosting Account Automation (cPanel & DirectAdmin)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Test command
    test_parser = subparsers.add_parser("test", help="Test connectivity to hosting server")
    test_parser.add_argument("--panel", choices=["cpanel", "directadmin"], required=True, help="Target panel")
    test_parser.set_defaults(func=handle_test)

    # Test SMTP command
    smtp_parser = subparsers.add_parser("test-smtp", help="Test Zimbra / SMTP connection & authentication")
    smtp_parser.add_argument("--recipient", help="Optional recipient email to send a test message to")
    smtp_parser.set_defaults(func=handle_test_smtp)

    # Test NIC command
    nic_parser = subparsers.add_parser("test-nic", help="Test login & access to nic.bt.bt registry portal")
    nic_parser.set_defaults(func=handle_test_nic)

    # WHOIS Lookup command
    whois_parser = subparsers.add_parser("whois", help="Perform public WHOIS lookup on nic.bt.bt")
    whois_parser.add_argument("--domain", required=True, help="Domain name (e.g. karunabhutantravel.bt)")
    whois_parser.set_defaults(func=handle_whois)

    # Register Domain command
    reg_parser = subparsers.add_parser("register-domain", help="Register or update domain on nic.bt.bt")
    reg_parser.add_argument("--domain", required=True, help="Domain name (e.g. example.bt)")
    reg_parser.add_argument("--name", required=True, help="Customer or Organization Name")
    reg_parser.add_argument("--email", required=True, help="Customer contact email")
    reg_parser.add_argument("--phone", default="+975", help="Customer telephone")
    reg_parser.add_argument("--address", default="Thimphu, Bhutan", help="Customer address")
    reg_parser.set_defaults(func=handle_register_domain)

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
    create_parser.add_argument("--register-nic", action="store_true", help="Register/update domain on nic.bt.bt for WHOIS")
    create_parser.add_argument("--customer-name", help="Customer/Organization name for WHOIS")
    create_parser.add_argument("--save", action="store_true", help="Save handover text to a file")
    create_parser.set_defaults(func=handle_create)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
