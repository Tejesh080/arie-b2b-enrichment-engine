"""What each transactional email says (Productization M6 Parts 13/15/16) —
separate from *how* it's sent (`arie.email.sender.EmailSender`). Every
method here builds one `EmailMessage` and hands it to the injected sender;
none of them raise on a delivery failure — see each call site
(`arie.invitations`, `arie.billing.service`, `arie.jobs.handlers`) for how
the `DeliveryResult` is used.

**No marketing email, no raw payloads.** Every template here answers a
specific transactional trigger a user caused or needs to know about; none
carry a provider's raw response, a Stripe object, or any secret — see each
method's own note on what it deliberately omits.

**Every value interpolated into `html_content` is escaped**, via :func:`_h`.
This is not defensive tidiness. Several of these values are tenant-controlled
free text — most importantly `organization_name`, which any owner sets to any
200 characters they like — and an invitation goes to an arbitrary email
address *outside* the organization, sent from ARIE's own verified sending
domain. Unescaped, an organization named
`</p><a href="https://evil.example">Reset your password</a><p>` would render
as working markup in a stranger's inbox, with ARIE's deliverability behind it.
Self-service signup (Part 10) is what makes that reachable by anyone with an
email address, so the escaping arrived with it.

The plain-text half needs no escaping — it is never parsed as markup — and
deliberately keeps the raw value so a name containing an ampersand reads
correctly there.
"""

from __future__ import annotations

from html import escape

from arie.email.sender import DeliveryResult, EmailMessage, EmailSender

__all__ = ["EmailNotifier"]


def _h(value: object) -> str:
    """HTML-escape one interpolated value, quotes included.

    `quote=True` matters for the URL interpolations: they land inside an
    `href="..."` attribute, where an unescaped `"` would end the attribute
    early and let the rest of the value become new attributes.
    """
    return escape(str(value), quote=True)


class EmailNotifier:
    def __init__(self, sender: EmailSender) -> None:
        self._sender = sender

    def send_invitation(
        self,
        *,
        to_email: str,
        organization_name: str,
        inviter_email: str,
        role: str,
        accept_url: str,
    ) -> DeliveryResult:
        subject = f"You've been invited to {organization_name} on ARIE"
        text = (
            f"{inviter_email} has invited you to join {organization_name} on ARIE as {role}.\n\n"
            f"Accept the invitation: {accept_url}\n\n"
            "This link expires in 7 days. If you weren't expecting this, you can ignore this email."
        )
        html = (
            f"<p>{_h(inviter_email)} has invited you to join "
            f"<strong>{_h(organization_name)}</strong> on ARIE as <strong>{_h(role)}</strong>.</p>"
            f'<p><a href="{_h(accept_url)}">Accept the invitation</a></p>'
            "<p>This link expires in 7 days. If you weren't expecting this, you can ignore this email.</p>"
        )
        return self._sender.send(
            EmailMessage(
                to_email=to_email,
                subject=subject,
                text_content=text,
                html_content=html,
                category="invitation",
            )
        )

    def send_review_required(
        self,
        *,
        to_email: str,
        organization_name: str,
        lead_reference: str,
        summary: str,
        review_url: str,
    ) -> DeliveryResult:
        """`summary` must be a short, safe description (e.g. "reject,
        confidence 0.46 below threshold") — never a raw provider payload or
        evidence value that might contain more of the lead's personal data
        than a reviewer-notification email needs to carry."""
        subject = f"[{organization_name}] Lead awaiting review — {lead_reference}"
        text = f"A lead needs human review in {organization_name}.\n\n{summary}\n\nReview it: {review_url}"
        html = (
            f"<p>A lead needs human review in <strong>{_h(organization_name)}</strong>.</p>"
            f"<p>{_h(summary)}</p>"
            f'<p><a href="{_h(review_url)}">Review it</a></p>'
        )
        return self._sender.send(
            EmailMessage(
                to_email=to_email,
                subject=subject,
                text_content=text,
                html_content=html,
                category="review_required",
            )
        )

    def send_usage_warning(
        self,
        *,
        to_email: str,
        organization_name: str,
        metric_label: str,
        used: float,
        limit: float,
        percent: float,
        billing_url: str,
    ) -> DeliveryResult:
        subject = f"[{organization_name}] Approaching your {metric_label} limit"
        text = (
            f"{organization_name} has used {used:.2g} of {limit:.2g} {metric_label} "
            f"({percent:.0%}) this billing period.\n\nManage your plan: {billing_url}"
        )
        html = (
            f"<p><strong>{_h(organization_name)}</strong> has used {used:.2g} of {limit:.2g} "
            f"{_h(metric_label)} ({percent:.0%}) this billing period.</p>"
            f'<p><a href="{_h(billing_url)}">Manage your plan</a></p>'
        )
        return self._sender.send(
            EmailMessage(
                to_email=to_email,
                subject=subject,
                text_content=text,
                html_content=html,
                category="usage_warning",
            )
        )

    def send_limit_reached(
        self, *, to_email: str, organization_name: str, metric_label: str, billing_url: str
    ) -> DeliveryResult:
        subject = f"[{organization_name}] {metric_label} limit reached"
        text = (
            f"{organization_name} has reached its {metric_label} limit for this billing period.\n\n"
            f"Upgrade your plan to continue: {billing_url}"
        )
        html = (
            f"<p><strong>{_h(organization_name)}</strong> has reached its {_h(metric_label)} "
            "limit for this billing period.</p>"
            f'<p><a href="{_h(billing_url)}">Upgrade your plan to continue</a></p>'
        )
        return self._sender.send(
            EmailMessage(
                to_email=to_email,
                subject=subject,
                text_content=text,
                html_content=html,
                category="limit_reached",
            )
        )

    def send_payment_problem(
        self, *, to_email: str, organization_name: str, reason: str, portal_url: str
    ) -> DeliveryResult:
        """`reason` is a short, sanitized label (e.g. "payment failed") —
        never a raw Stripe decline code or card-adjacent detail."""
        subject = f"[{organization_name}] Payment problem on your ARIE subscription"
        text = (
            f"There was a problem with {organization_name}'s subscription payment: {reason}.\n\n"
            f"Update your payment method: {portal_url}"
        )
        html = (
            f"<p>There was a problem with <strong>{_h(organization_name)}</strong>'s subscription "
            f"payment: {_h(reason)}.</p>"
            f'<p><a href="{_h(portal_url)}">Update your payment method</a></p>'
        )
        return self._sender.send(
            EmailMessage(
                to_email=to_email,
                subject=subject,
                text_content=text,
                html_content=html,
                category="payment_problem",
            )
        )

    def send_security_notice(
        self, *, to_email: str, organization_name: str, message: str
    ) -> DeliveryResult:
        subject = f"[{organization_name}] Security notice"
        return self._sender.send(
            EmailMessage(
                to_email=to_email,
                subject=subject,
                text_content=message,
                html_content=f"<p>{_h(message)}</p>",
                category="security_notice",
            )
        )
