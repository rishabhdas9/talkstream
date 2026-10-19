import { useState } from "react";

import { rechargeWallet, type WalletResponse } from "../../api/auth";
import { isAuthenticationExpiredError } from "../../api/errors";
import { formatDate, formatRupees } from "../format";

type WalletPageProps = {
  token: string;
  wallet: WalletResponse | null;
  onWalletChanged: () => void;
  pushToast: (message: string) => void;
  openSession: (sessionId: string) => void;
  onAuthenticationExpired: () => void;
};

const HEADER_CELL = {
  textAlign: "left" as const,
  fontSize: 11,
  fontWeight: 500,
  letterSpacing: "0.07em",
  textTransform: "uppercase" as const,
  color: "#6B6B66",
  padding: "8px 12px",
  borderBottom: "1px solid #E5E5E1",
};

export function WalletPage({
  token,
  wallet,
  onWalletChanged,
  pushToast,
  openSession,
  onAuthenticationExpired,
}: WalletPageProps) {
  const [customAmount, setCustomAmount] = useState("");
  const [busy, setBusy] = useState(false);

  const balance = wallet?.balance_paise ?? 0;
  const pricePerMinute = wallet?.price_per_minute_paise ?? 200;
  const transactions = wallet?.recent_transactions ?? [];

  // Running balance per row, walked back from the current balance across
  // the (newest-first) listed transactions. Billing writes one ledger row
  // per tick, so usage rows are folded into one display row per session.
  type Row = { txn: (typeof transactions)[number]; balanceAfter: number };
  let running = balance;
  const rows: Row[] = [];
  const usageBySession = new Map<string, Row>();
  for (const txn of transactions) {
    const balanceAfter = running;
    running -= txn.amount_paise;
    const sessionId = txn.kind === "usage" ? txn.usage_session_id : null;
    if (sessionId) {
      const group = usageBySession.get(sessionId);
      if (group) {
        group.txn = { ...group.txn, amount_paise: group.txn.amount_paise + txn.amount_paise };
        continue;
      }
      const entry: Row = { txn: { ...txn }, balanceAfter };
      usageBySession.set(sessionId, entry);
      rows.push(entry);
      continue;
    }
    rows.push({ txn, balanceAfter });
  }

  const customPaise =
    Number.parseInt(customAmount, 10) > 0 ? Number.parseInt(customAmount, 10) * 100 : 0;
  const maxTopup = wallet?.max_recharge_paise ?? 0;
  const topupUsed = wallet?.topups_left_today === 0;
  const canTopup = wallet != null && !topupUsed && !busy;
  const presets = [Math.round(maxTopup / 2), maxTopup].filter((amount, index, all) => amount > 0 && all.indexOf(amount) === index);

  async function topup(amountPaise: number) {
    if (!amountPaise || amountPaise <= 0 || !canTopup) return;
    if (amountPaise > maxTopup) {
      pushToast(`A top-up can be at most ${formatRupees(maxTopup)}`);
      return;
    }
    setBusy(true);
    try {
      await rechargeWallet(token, amountPaise);
      onWalletChanged();
      setCustomAmount("");
      pushToast(`${formatRupees(amountPaise)} added to your wallet`);
    } catch (error) {
      if (isAuthenticationExpiredError(error)) onAuthenticationExpired();
      else pushToast(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy(false);
    }
  }

  const presetStyle = {
    border: "1px solid #E5E5E1",
    background: "#FFFFFF",
    borderRadius: 999,
    padding: "8px 18px",
    fontSize: 13.5,
    fontWeight: 500,
  };

  return (
    <div className="content-page wallet-page" style={{ padding: "var(--page-gutter)", maxWidth: 1000, margin: "0 auto" }}>
      <h1 style={{ margin: 0, fontSize: 26, fontWeight: 500, letterSpacing: "-0.02em" }}>
        Wallet
      </h1>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "var(--wallet-columns)",
          gap: 16,
          marginTop: 24,
          alignItems: "stretch",
        }}
      >
        <div
          style={{
            background: "#FFFFFF",
            border: "1px solid #E5E5E1",
            borderRadius: 14,
            padding: 24,
          }}
        >
          <div
            style={{
              fontSize: 11,
              fontWeight: 600,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              color: "#6B6B66",
            }}
          >
            Balance
          </div>
          <div
            style={{
              fontSize: 44,
              fontWeight: 500,
              letterSpacing: "-0.03em",
              marginTop: 10,
              fontVariantNumeric: "tabular-nums",
            }}
          >
            {wallet == null ? "—" : formatRupees(balance)}
          </div>
          <div style={{ fontSize: 12.5, color: "#6B6B66", marginTop: 8 }}>
            {formatRupees(pricePerMinute)}/min · billed per second
          </div>
          {wallet != null && wallet.daily_allowance_paise > 0 && (
            <div style={{ fontSize: 12.5, color: "#6B6B66", marginTop: 4 }}>
              Resets to {formatRupees(wallet.daily_allowance_paise)} every day at midnight IST
            </div>
          )}
          <div style={{ fontSize: 12.5, color: "#3A57D4", fontWeight: 500, marginTop: 4 }}>
            ≈ {Math.floor(balance / pricePerMinute)} min of talk time left
          </div>
        </div>

        <div
          style={{
            background: "#FFFFFF",
            border: "1px solid #E5E5E1",
            borderRadius: 14,
            padding: 24,
          }}
        >
          <div
            style={{
              fontSize: 11,
              fontWeight: 600,
              letterSpacing: "0.08em",
              textTransform: "uppercase",
              color: "#6B6B66",
            }}
          >
            Add money
          </div>
          <div className="wallet-presets" style={{ display: "flex", gap: 8, marginTop: 14 }}>
            {presets.map((amount) => (
              <button key={amount} type="button" className="hovBorderDark" style={{ ...presetStyle, opacity: canTopup ? 1 : 0.5 }} disabled={!canTopup} onClick={() => void topup(amount)}>
                {formatRupees(amount)}
              </button>
            ))}
          </div>
          <div className="wallet-custom-amount" style={{ display: "flex", gap: 8, marginTop: 12 }}>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                flex: 1,
                border: "1px solid #E5E5E1",
                borderRadius: 999,
                padding: "0 4px 0 14px",
                background: "#F7F7F5",
              }}
            >
              <span style={{ fontSize: 13.5, color: "#6B6B66" }}>₹</span>
              <input
                value={customAmount}
                onChange={(event) =>
                  setCustomAmount(event.target.value.replace(/\D/g, "").slice(0, 5))
                }
                onKeyDown={(event) => {
                  if (event.key === "Enter") void topup(customPaise);
                }}
                placeholder={maxTopup ? `Up to ${formatRupees(maxTopup)}` : "Custom amount"}
                aria-label="Top-up amount in rupees"
                disabled={!canTopup}
                inputMode="numeric"
                style={{
                  flex: 1,
                  minWidth: 0,
                  border: "none",
                  background: "none",
                  padding: "9px 8px",
                  fontSize: 13.5,
                }}
              />
            </div>
            <button
              type="button"
              onClick={() => void topup(customPaise)}
              disabled={!canTopup}
              className="hovDark"
              style={{
                border: "none",
                borderRadius: 999,
                background: "#111110",
                color: "#FFFFFF",
                fontSize: 13.5,
                fontWeight: 500,
                padding: "9px 20px",
                flexShrink: 0,
                opacity: canTopup ? 1 : 0.5,
              }}
            >
              {customPaise ? `Add ${formatRupees(customPaise)}` : "Add money"}
            </button>
          </div>
          <div style={{ fontSize: 11.5, color: "#A6A6A0", marginTop: 12 }}>
            {topupUsed
              ? "You've used today's top-up. Your wallet resets at midnight IST."
              : wallet?.topups_left_today === 1
                ? "One top-up per day. Demo wallet: no payment needed."
                : "Demo wallet: credits instantly, no payment needed."}
          </div>
        </div>
      </div>

      <div
        style={{
          background: "#FFFFFF",
          border: "1px solid #E5E5E1",
          borderRadius: 14,
          marginTop: 16,
          overflow: "hidden",
        }}
      >
        <div style={{ padding: "18px 20px 12px", fontSize: 13.5, fontWeight: 600 }}>
          Transactions
        </div>
        {rows.length === 0 ? (
          <div style={{ padding: "48px 20px", textAlign: "center" }}>
            <div
              style={{
                width: 44,
                height: 44,
                borderRadius: "50%",
                background: "#F7F7F5",
                border: "1px solid #E5E5E1",
                margin: "0 auto",
              }}
            />
            <div style={{ marginTop: 14, fontSize: 13.5, fontWeight: 500 }}>
              No transactions yet
            </div>
            <div style={{ marginTop: 5, fontSize: 12.5, color: "#A6A6A0" }}>
              Add money to get started.
            </div>
          </div>
        ) : (
          <><div className="table-scroll-hint">Swipe to see amounts and balances →</div><div className="table-scroll"><table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                <th style={{ ...HEADER_CELL, padding: "8px 20px" }}>Date</th>
                <th style={HEADER_CELL}>Type</th>
                <th style={HEADER_CELL}>Session</th>
                <th style={{ ...HEADER_CELL, textAlign: "right" }}>Amount</th>
                <th style={{ ...HEADER_CELL, textAlign: "right", padding: "8px 20px" }}>
                  Balance
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ txn, balanceAfter }) => (
                <tr key={txn.id} className="hovRow">
                  <td
                    style={{
                      padding: "11px 20px",
                      fontSize: 13,
                      borderBottom: "1px solid #F4F4F0",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {formatDate(txn.created_at)}
                  </td>
                  <td style={{ padding: "11px 12px", borderBottom: "1px solid #F4F4F0" }}>
                    <span
                      style={{
                        fontSize: 11.5,
                        fontWeight: 500,
                        borderRadius: 999,
                        padding: "3px 10px",
                        background: txn.kind === "topup" ? "#E8F3EC" : "#F1F1ED",
                        color: txn.kind === "topup" ? "#1F7A46" : "#5C5C57",
                      }}
                    >
                      {txn.kind === "topup" ? "Top-up" : "Usage"}
                    </span>
                  </td>
                  <td style={{ padding: "11px 12px", borderBottom: "1px solid #F4F4F0" }}>
                    {txn.usage_session_id ? (
                      <button
                        type="button"
                        onClick={() => openSession(txn.usage_session_id!)}
                        title="View session in Usage"
                        className="hovUnderline"
                        style={{
                          border: "none",
                          background: "none",
                          padding: 0,
                          fontSize: 12,
                          fontFamily: "ui-monospace,'SF Mono',monospace",
                          color: "#4A6CF7",
                        }}
                      >
                        {txn.usage_session_id.slice(0, 8)}
                      </button>
                    ) : (
                      <span
                        style={{
                          fontSize: 12,
                          fontFamily: "ui-monospace,'SF Mono',monospace",
                          color: "#6B6B66",
                        }}
                      >
                        —
                      </span>
                    )}
                  </td>
                  <td
                    style={{
                      padding: "11px 12px",
                      fontSize: 13,
                      textAlign: "right",
                      fontVariantNumeric: "tabular-nums",
                      fontWeight: 500,
                      color: txn.amount_paise > 0 ? "#1F7A46" : "#B3352E",
                      borderBottom: "1px solid #F4F4F0",
                    }}
                  >
                    {txn.amount_paise > 0 ? "+" : "−"}
                    {formatRupees(Math.abs(txn.amount_paise))}
                  </td>
                  <td
                    style={{
                      padding: "11px 20px",
                      fontSize: 13,
                      textAlign: "right",
                      fontVariantNumeric: "tabular-nums",
                      color: "#6B6B66",
                      borderBottom: "1px solid #F4F4F0",
                    }}
                  >
                    {formatRupees(balanceAfter)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table></div></>
        )}
      </div>
    </div>
  );
}
