# ============================================================
# alerts/notifier.py
#
# Alert System
# ──────────────────────────────────────────────────────────
# Sends high-confidence trade signals via:
#  - SMS    (Twilio)
#  - Email  (SendGrid or SMTP)
#
# Alert rules:
#  1. Only fire if signal.confidence ≥ AlertConfig.min_score_for_alert
#  2. Cooldown per symbol to prevent alert spam
#  3. Rich formatting with entry/SL/TP/R:R and reasoning
# ============================================================

import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from loguru import logger

from config.settings import CONFIG
from strategy.engine import TradeSignal


# ────────────────────────────────────────────────
# BASE ALERT CHANNEL
# ────────────────────────────────────────────────

class AlertChannel:
    """Abstract base for alert delivery channels."""

    def send(self, signal: TradeSignal) -> bool:
        raise NotImplementedError


# ────────────────────────────────────────────────
# SMS ALERT (Twilio)
# ────────────────────────────────────────────────

class SMSAlert(AlertChannel):
    """
    Sends SMS alerts via Twilio.
    Free tier: ~$1 per 100 messages.
    """

    def __init__(self):
        self.cfg = CONFIG.alerts

    def send(self, signal: TradeSignal) -> bool:
        if not all([self.cfg.twilio_sid, self.cfg.twilio_token,
                    self.cfg.twilio_from, self.cfg.alert_phone]):
            logger.warning("Twilio credentials not configured — SMS skipped")
            return False

        try:
            from twilio.rest import Client
            client = Client(self.cfg.twilio_sid, self.cfg.twilio_token)

            message_body = self._format_sms(signal)

            msg = client.messages.create(
                body=message_body,
                from_=self.cfg.twilio_from,
                to=self.cfg.alert_phone,
            )
            logger.info(f"SMS sent: SID={msg.sid}")
            return True

        except ImportError:
            logger.warning("twilio package not installed — SMS disabled")
            return False
        except Exception as e:
            logger.error(f"SMS send failed: {e}")
            return False

    @staticmethod
    def _format_sms(signal: TradeSignal) -> str:
        """
        Compact SMS format — keeps it under 160 chars.
        Longer signals split into 2 messages automatically by Twilio.
        """
        direction_emoji = "🟢" if signal.direction.value == "BUY" else "🔴"
        return (
            f"{direction_emoji} TRADE SIGNAL\n"
            f"{signal.symbol} {signal.direction.value}\n"
            f"Entry: {signal.entry_price:.5f}\n"
            f"SL: {signal.stop_loss:.5f}\n"
            f"TP: {signal.take_profit:.5f}\n"
            f"R:R {signal.risk_reward:.1f} | Conf: {signal.confidence:.0f}%\n"
            f"{signal.timestamp.strftime('%H:%M UTC')}"
        )


# ────────────────────────────────────────────────
# EMAIL ALERT (SendGrid + SMTP fallback)
# ────────────────────────────────────────────────

class EmailAlert(AlertChannel):
    """
    Rich HTML email alerts with full signal breakdown.
    Primary: SendGrid API
    Fallback: SMTP (Gmail/Outlook)
    """

    def __init__(self):
        self.cfg = CONFIG.alerts

    def send(self, signal: TradeSignal) -> bool:
        if self.cfg.sendgrid_api_key:
            return self._send_sendgrid(signal)
        else:
            logger.warning("SendGrid not configured — email disabled")
            return False

    def _send_sendgrid(self, signal: TradeSignal) -> bool:
        try:
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail

            sg = SendGridAPIClient(self.cfg.sendgrid_api_key)
            message = Mail(
                from_email=self.cfg.email_from,
                to_emails=self.cfg.email_to,
                subject=self._format_subject(signal),
                html_content=self._format_html(signal),
            )
            response = sg.send(message)
            logger.info(f"Email sent: status={response.status_code}")
            return response.status_code in (200, 202)
        except ImportError:
            logger.warning("sendgrid package not installed — email disabled")
            return False
        except Exception as e:
            logger.error(f"Email send failed: {e}")
            return False

    def _send_smtp(self, signal: TradeSignal, smtp_host: str,
                   smtp_port: int, username: str, password: str) -> bool:
        """SMTP fallback (e.g. Gmail with app password)."""
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = self._format_subject(signal)
            msg["From"] = self.cfg.email_from
            msg["To"] = self.cfg.email_to
            msg.attach(MIMEText(self._format_html(signal), "html"))

            with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                server.login(username, password)
                server.sendmail(self.cfg.email_from, self.cfg.email_to, msg.as_string())
            return True
        except Exception as e:
            logger.error(f"SMTP send failed: {e}")
            return False

    @staticmethod
    def _format_subject(signal: TradeSignal) -> str:
        direction_label = "📈 BUY" if signal.direction.value == "BUY" else "📉 SELL"
        return (f"{direction_label} Signal: {signal.symbol} | "
                f"Confidence: {signal.confidence:.0f}% | "
                f"R:R {signal.risk_reward:.1f}")

    @staticmethod
    def _format_html(signal: TradeSignal) -> str:
        """Generates a clean HTML email with signal details."""
        direction_color = "#00C851" if signal.direction.value == "BUY" else "#ff4444"
        direction_bg = "#e8f5e9" if signal.direction.value == "BUY" else "#ffebee"
        reasoning_html = "".join(f"<li>{r}</li>" for r in signal.reasoning)

        return f"""
<!DOCTYPE html>
<html>
<head>
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 20px; }}
  .card {{ background: white; border-radius: 12px; padding: 24px; max-width: 560px;
           margin: 0 auto; box-shadow: 0 2px 12px rgba(0,0,0,0.1); }}
  .header {{ background: {direction_bg}; border-left: 4px solid {direction_color};
             border-radius: 8px; padding: 16px; margin-bottom: 20px; }}
  .direction {{ font-size: 28px; font-weight: bold; color: {direction_color}; }}
  .symbol {{ font-size: 20px; color: #333; margin-top: 4px; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 16px 0; }}
  .metric {{ background: #f8f9fa; border-radius: 8px; padding: 12px; }}
  .metric-label {{ font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 0.5px; }}
  .metric-value {{ font-size: 18px; font-weight: bold; color: #333; margin-top: 2px; }}
  .confidence-bar {{ background: #e0e0e0; border-radius: 4px; height: 8px; margin-top: 6px; }}
  .confidence-fill {{ background: {direction_color}; border-radius: 4px; height: 8px;
                      width: {signal.confidence:.0f}%; }}
  .reasoning {{ background: #f8f9fa; border-radius: 8px; padding: 16px; margin-top: 16px; }}
  .reasoning h3 {{ margin: 0 0 8px; font-size: 13px; color: #666; text-transform: uppercase; }}
  .reasoning ul {{ margin: 0; padding-left: 20px; }}
  .reasoning li {{ color: #444; font-size: 13px; line-height: 1.6; }}
  .footer {{ text-align: center; color: #999; font-size: 11px; margin-top: 16px; }}
</style>
</head>
<body>
<div class="card">
  <div class="header">
    <div class="direction">{'📈' if signal.direction.value == 'BUY' else '📉'} {signal.direction.value}</div>
    <div class="symbol">{signal.symbol} &nbsp;·&nbsp; {signal.timeframe}</div>
  </div>

  <div class="grid">
    <div class="metric">
      <div class="metric-label">Entry Price</div>
      <div class="metric-value">{signal.entry_price:.5f}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Stop Loss</div>
      <div class="metric-value" style="color: #ff4444">{signal.stop_loss:.5f}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Take Profit</div>
      <div class="metric-value" style="color: #00C851">{signal.take_profit:.5f}</div>
    </div>
    <div class="metric">
      <div class="metric-label">Risk/Reward</div>
      <div class="metric-value">{signal.risk_reward:.1f} : 1</div>
    </div>
  </div>

  <div class="metric">
    <div class="metric-label">Confidence Score</div>
    <div class="metric-value">{signal.confidence:.1f}%</div>
    <div class="confidence-bar"><div class="confidence-fill"></div></div>
  </div>

  <div class="grid">
    <div class="metric">
      <div class="metric-label">ML Probability</div>
      <div class="metric-value">{signal.ml_probability*100:.1f}%</div>
    </div>
    <div class="metric">
      <div class="metric-label">Sentiment</div>
      <div class="metric-value">{'+' if signal.sentiment_score >= 0 else ''}{signal.sentiment_score:.2f}</div>
    </div>
  </div>

  <div class="reasoning">
    <h3>Signal Reasoning</h3>
    <ul>{reasoning_html}</ul>
  </div>

  <div class="footer">
    Signal ID: {signal.signal_id} &nbsp;·&nbsp;
    {signal.timestamp.strftime('%Y-%m-%d %H:%M UTC')}<br>
    <strong>Not financial advice. Always manage risk appropriately.</strong>
  </div>
</div>
</body>
</html>
"""


# ────────────────────────────────────────────────
# ALERT MANAGER
# ────────────────────────────────────────────────

class AlertManager:
    """
    Orchestrates all alert channels with:
    - Confidence threshold gating
    - Per-symbol cooldown to prevent spam
    - Delivery confirmation logging
    """

    def __init__(self):
        self.cfg = CONFIG.alerts
        self.channels: List[AlertChannel] = [
            SMSAlert(),
            EmailAlert(),
        ]
        # Track last alert time per symbol: {symbol: timestamp}
        self._last_alert: Dict[str, float] = {}

    def process_signal(self, signal: TradeSignal) -> bool:
        """
        Main entry point. Processes a signal and fires alerts if appropriate.

        Returns True if alerts were sent.
        """
        # Gate 1: Confidence threshold
        if signal.confidence < self.cfg.min_score_for_alert:
            logger.debug(
                f"Signal {signal.signal_id} below alert threshold "
                f"({signal.confidence:.1f}% < {self.cfg.min_score_for_alert}%)"
            )
            return False

        # Gate 2: Cooldown (prevent duplicate alerts for same symbol)
        now = time.time()
        cooldown_secs = self.cfg.cooldown_minutes * 60
        last_time = self._last_alert.get(signal.symbol, 0)
        if now - last_time < cooldown_secs:
            remaining = int((cooldown_secs - (now - last_time)) / 60)
            logger.debug(f"Alert cooldown active for {signal.symbol}: {remaining}min remaining")
            return False

        # Fire all channels
        sent = False
        for channel in self.channels:
            try:
                success = channel.send(signal)
                if success:
                    sent = True
                    logger.info(f"Alert sent via {channel.__class__.__name__}: {signal.symbol}")
            except Exception as e:
                logger.error(f"Alert channel {channel.__class__.__name__} failed: {e}")

        if sent:
            self._last_alert[signal.symbol] = now

        return sent

    def add_channel(self, channel: AlertChannel):
        """Allows adding custom alert channels (e.g. Telegram, Discord, Slack)."""
        self.channels.append(channel)
        logger.info(f"Alert channel added: {channel.__class__.__name__}")
