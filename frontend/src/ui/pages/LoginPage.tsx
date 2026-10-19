import {
  type ChangeEvent,
  type ClipboardEvent,
  type KeyboardEvent,
  type MouseEvent,
  useEffect,
  useRef,
  useState,
} from "react";

import { devLogin, requestOtp, verifyOtp } from "../../api/auth";
import { getCapabilities } from "../../api/capabilities";

type LoginPageProps = {
  onAuthenticated: (token: string, email: string) => void;
  pushToast: (message: string) => void;
};

const HERO_BARS = Array.from({ length: 36 }, (_, i) => ({
  color: `oklch(0.62 0.17 ${(262 - i * 2.6).toFixed(0)})`,
  delay: `${(-(i * 0.11)).toFixed(2)}s`,
}));

function Logo({ size }: { size: "small" | "card" }) {
  const box = size === "card" ? 40 : 22;
  const radius = size === "card" ? 12 : 7;
  const bars = size === "card" ? [12, 19, 9] : [7, 11, 5];
  const barWidth = size === "card" ? 3 : 2;
  return (
    <div className="login-page"
      style={{
        width: box,
        height: box,
        borderRadius: radius,
        background: "#111110",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        gap: size === "card" ? 3 : 2,
        margin: size === "card" ? "0 auto" : undefined,
      }}
    >
      {bars.map((height, index) => (
        <span
          key={index}
          style={{
            width: barWidth,
            height,
            borderRadius: barWidth / 2,
            background: "#FFFFFF",
          }}
        />
      ))}
    </div>
  );
}

export function LoginPage({ onAuthenticated, pushToast }: LoginPageProps) {
  const [emailInput, setEmailInput] = useState("");
  const [otpStep, setOtpStep] = useState(false);
  const [otp, setOtp] = useState<string[]>(["", "", "", "", "", ""]);
  const [otpError, setOtpError] = useState("");
  const [busy, setBusy] = useState(false);
  const [resendIn, setResendIn] = useState(0);
  const [mouse, setMouse] = useState({ x: 0, y: 0 });
  const boxRefs = useRef<Array<HTMLInputElement | null>>([]);
  const [localLogin, setLocalLogin] = useState(false);

  useEffect(() => {
    let active = true;
    getCapabilities()
      .then((capabilities) => { if (active) setLocalLogin(capabilities.dev_login === true); })
      .catch(() => undefined);
    return () => { active = false; };
  }, []);

  async function signInLocally() {
    setBusy(true);
    try {
      onAuthenticated(await devLogin(), "user");
    } catch (error) {
      pushToast(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (resendIn <= 0) return;
    const timer = window.setTimeout(() => setResendIn((value) => value - 1), 1000);
    return () => window.clearTimeout(timer);
  }, [resendIn]);

  async function sendCode() {
    const email = emailInput.trim();
    if (!email || email.indexOf("@") < 1) {
      pushToast("Enter a valid email address");
      return;
    }
    setBusy(true);
    try {
      await requestOtp(email);
      setOtpStep(true);
      setOtp(["", "", "", "", "", ""]);
      setOtpError("");
      setResendIn(30);
    } catch (error) {
      pushToast(String(error instanceof Error ? error.message : error));
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(code: string) {
    setBusy(true);
    try {
      const token = await verifyOtp(emailInput.trim(), code);
      onAuthenticated(token, emailInput.trim());
    } catch (error) {
      setOtpError(String(error instanceof Error ? error.message : error));
      setOtp(["", "", "", "", "", ""]);
      boxRefs.current[0]?.focus();
    } finally {
      setBusy(false);
    }
  }

  // Distribute a pasted or autofilled code across the boxes, starting from
  // the first box no matter which one received it.
  function fillOtp(digits: string) {
    const code = digits.replace(/\D/g, "").slice(0, 6);
    if (!code) return;
    const next = Array.from({ length: 6 }, (_, i) => code[i] ?? "");
    setOtp(next);
    setOtpError("");
    boxRefs.current[Math.min(code.length, 5)]?.focus();
    if (code.length === 6) void submitCode(code);
  }

  function onOtpInput(index: number) {
    return (event: ChangeEvent<HTMLInputElement>) => {
      const digits = (event.target.value || "").replace(/\D/g, "");
      // Mobile keyboards and password managers can insert the whole code as
      // one value change without a paste event.
      if (digits.length > 1) {
        fillOtp(digits);
        return;
      }
      const value = digits.slice(-1);
      const next = otp.slice();
      next[index] = value;
      setOtp(next);
      setOtpError("");
      if (value && index < 5) boxRefs.current[index + 1]?.focus();
      if (next.every((digit) => digit !== "")) void submitCode(next.join(""));
    };
  }

  function onOtpPaste(event: ClipboardEvent<HTMLInputElement>) {
    event.preventDefault();
    fillOtp(event.clipboardData.getData("text"));
  }

  function onOtpKey(index: number) {
    return (event: KeyboardEvent<HTMLInputElement>) => {
      if (event.key === "Backspace" && !otp[index] && index > 0) {
        boxRefs.current[index - 1]?.focus();
      }
    };
  }

  function onMove(event: MouseEvent<HTMLDivElement>) {
    const nx = (event.clientX / Math.max(window.innerWidth, 1)) * 2 - 1;
    const ny = (event.clientY / Math.max(window.innerHeight, 1)) * 2 - 1;
    if (Math.abs(nx - mouse.x) > 0.04 || Math.abs(ny - mouse.y) > 0.04) {
      setMouse({ x: nx, y: ny });
    }
  }

  return (
    <div
      onMouseMove={onMove}
      style={{
        minHeight: "100vh",
        display: "flex",
        flexDirection: "column",
        background: "#F7F7F5",
        position: "relative",
        overflow: "hidden",
      }}
    >
      <div style={{ position: "absolute", inset: 0, overflow: "hidden", pointerEvents: "none" }}>
        <div
          style={{
            position: "absolute",
            top: -120,
            left: -100,
            transform: `translate(${(mouse.x * 30).toFixed(1)}px, ${(mouse.y * 20).toFixed(1)}px)`,
            transition: "transform .7s ease-out",
          }}
        >
          <div
            style={{
              width: 440,
              height: 440,
              borderRadius: "50%",
              background: "oklch(0.62 0.17 250)",
              opacity: 0.09,
              filter: "blur(64px)",
              animation: "blobDrift1 18s ease-in-out infinite",
            }}
          />
        </div>
        <div
          style={{
            position: "absolute",
            bottom: -140,
            right: -90,
            transform: `translate(${(mouse.x * -36).toFixed(1)}px, ${(mouse.y * -24).toFixed(1)}px)`,
            transition: "transform .7s ease-out",
          }}
        >
          <div
            style={{
              width: 400,
              height: 400,
              borderRadius: "50%",
              background: "oklch(0.62 0.17 180)",
              opacity: 0.1,
              filter: "blur(64px)",
              animation: "blobDrift2 22s ease-in-out infinite",
            }}
          />
        </div>
        <div
          style={{
            position: "absolute",
            top: "28%",
            right: "16%",
            transform: `translate(${(mouse.x * 18).toFixed(1)}px, ${(mouse.y * 26).toFixed(1)}px)`,
            transition: "transform .7s ease-out",
          }}
        >
          <div
            style={{
              width: 250,
              height: 250,
              borderRadius: "50%",
              background: "oklch(0.62 0.17 215)",
              opacity: 0.08,
              filter: "blur(52px)",
              animation: "blobDrift3 26s ease-in-out infinite",
            }}
          />
        </div>
      </div>

      <header className="login-header"
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "18px 32px",
          position: "relative",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
          <Logo size="small" />
          <div style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-0.01em" }}>
            Talkstream<span style={{ color: "#4A6CF7" }}>AI</span>
          </div>
        </div>
        <button
          type="button"
          onClick={localLogin ? () => void signInLocally() : undefined}
          disabled={busy}
          className="hovDark"
          style={{
            border: "none",
            borderRadius: 999,
            background: "#111110",
            color: "#FFFFFF",
            fontSize: 13,
            fontWeight: 500,
            padding: "8px 18px",
          }}
        >
          Log in
        </button>
      </header>

      <div className="login-content"
        style={{
          flex: 1,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 72,
          padding: "32px 64px",
          position: "relative",
          flexWrap: "wrap",
        }}
      >
        <div className="login-intro" style={{ flex: 1, minWidth: 340, maxWidth: 560 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 4, height: 56 }}>
            {HERO_BARS.map((bar, index) => (
              <div
                key={index}
                style={{
                  width: 4,
                  height: 52,
                  borderRadius: 2,
                  background: bar.color,
                  transformOrigin: "center",
                  animation: "waveListen 2.6s ease-in-out infinite",
                  animationDelay: bar.delay,
                }}
              />
            ))}
          </div>
          <h1
            style={{
              margin: "30px 0 0",
              fontSize: 46,
              fontWeight: 500,
              letterSpacing: "-0.03em",
              lineHeight: 1.12,
            }}
          >
            Talkstream<span style={{ color: "#4A6CF7" }}>AI</span> — Bas boliye.
          </h1>
          <p
            style={{
              margin: "18px 0 0",
              fontSize: 16.5,
              color: "#6B6B66",
              lineHeight: 1.65,
              maxWidth: 420,
              textWrap: "pretty",
            }}
          >
            Ask questions, manage todos, search the web — all by voice.
          </p>
        </div>

        <div className="login-card"
          style={{
            flexShrink: 0,
            width: 400,
            maxWidth: "100%",
            background: "#FFFFFF",
            border: "1px solid #E5E5E1",
            borderRadius: 16,
            padding: 36,
            boxSizing: "border-box",
            boxShadow: "0 24px 48px -32px rgba(17,17,16,0.18)",
          }}
        >
          <Logo size="card" />
          <div
            style={{
              marginTop: 18,
              fontSize: 19,
              fontWeight: 600,
              textAlign: "center",
              letterSpacing: "-0.01em",
            }}
          >
            Sign in to Talkstream<span style={{ color: "#4A6CF7" }}>AI</span>
          </div>

          {localLogin && (
            <>
              <div style={{ marginTop: 8, fontSize: 13, color: "#6B6B66", textAlign: "center" }}>
                Local mode: you sign in as <span style={{ color: "#111110", fontWeight: 500 }}>user</span>.
              </div>
              <button
                type="button"
                onClick={() => void signInLocally()}
                disabled={busy}
                className="hovDark"
                style={{
                  width: "100%",
                  marginTop: 24,
                  border: "none",
                  borderRadius: 999,
                  background: "#111110",
                  color: "#FFFFFF",
                  fontSize: 14,
                  fontWeight: 500,
                  padding: 12,
                  opacity: busy ? 0.7 : 1,
                }}
              >
                {busy ? "Signing in…" : "Log in"}
              </button>
            </>
          )}

          {!localLogin && !otpStep && (
            <>
              <div style={{ marginTop: 8, fontSize: 13, color: "#6B6B66", textAlign: "center" }}>
                We'll email you a 6-digit code — no password needed.
              </div>
              <div style={{ marginTop: 24, fontSize: 12, fontWeight: 500, color: "#6B6B66" }}>
                Email
              </div>
              <input
                type="email"
                value={emailInput}
                onChange={(event) => setEmailInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") void sendCode();
                }}
                placeholder="you@example.com"
                className="inputFocus"
                style={{
                  width: "100%",
                  marginTop: 6,
                  border: "1px solid #E5E5E1",
                  borderRadius: 10,
                  padding: "11px 14px",
                  fontSize: 14,
                  background: "#F7F7F5",
                }}
              />
              <button
                type="button"
                onClick={() => void sendCode()}
                disabled={busy}
                className="hovDark"
                style={{
                  width: "100%",
                  marginTop: 14,
                  border: "none",
                  borderRadius: 999,
                  background: "#111110",
                  color: "#FFFFFF",
                  fontSize: 14,
                  fontWeight: 500,
                  padding: 12,
                  opacity: busy ? 0.7 : 1,
                }}
              >
                {busy ? "Sending…" : "Send code"}
              </button>
            </>
          )}

          {!localLogin && otpStep && (
            <>
              <div style={{ marginTop: 8, fontSize: 13, color: "#6B6B66", textAlign: "center" }}>
                Enter the code sent to{" "}
                <span style={{ color: "#111110", fontWeight: 500 }}>{emailInput.trim()}</span>
              </div>
              <div style={{ display: "flex", justifyContent: "center", gap: 8, marginTop: 24 }}>
                {otp.map((value, index) => (
                  <input
                    key={index}
                    ref={(element) => {
                      boxRefs.current[index] = element;
                    }}
                    value={value}
                    onChange={onOtpInput(index)}
                    onKeyDown={onOtpKey(index)}
                    onPaste={onOtpPaste}
                    inputMode="numeric"
                    autoComplete={index === 0 ? "one-time-code" : "off"}
                    className="otpFocus"
                    style={{
                      width: 44,
                      height: 52,
                      textAlign: "center",
                      fontSize: 20,
                      fontWeight: 500,
                      border: `1px solid ${otpError ? "#E4A9A4" : "#E5E5E1"}`,
                      borderRadius: 10,
                      background: "#FFFFFF",
                      padding: 0,
                    }}
                  />
                ))}
              </div>
              {otpError && (
                <>
                  <div
                    style={{
                      marginTop: 14,
                      textAlign: "center",
                      fontSize: 12.5,
                      color: "#C2362F",
                      fontWeight: 500,
                    }}
                  >
                    Wrong or expired code
                  </div>
                  <div style={{ marginTop: 3, textAlign: "center", fontSize: 12, color: "#A6A6A0" }}>
                    {otpError}
                  </div>
                </>
              )}
              <div style={{ marginTop: 18, textAlign: "center", fontSize: 12.5 }}>
                {resendIn <= 0 ? (
                  <button
                    type="button"
                    onClick={() => {
                      void sendCode();
                      pushToast(`Code sent to ${emailInput.trim()}`);
                    }}
                    className="hovUnderline"
                    style={{
                      border: "none",
                      background: "none",
                      color: "#4A6CF7",
                      fontSize: 12.5,
                      padding: 0,
                    }}
                  >
                    Resend code
                  </button>
                ) : (
                  <span style={{ color: "#A6A6A0" }}>Resend in {resendIn}s</span>
                )}
              </div>
              <div style={{ marginTop: 10, textAlign: "center" }}>
                <button
                  type="button"
                  onClick={() => {
                    setOtpStep(false);
                    setOtp(["", "", "", "", "", ""]);
                    setOtpError("");
                  }}
                  className="hovFg"
                  style={{
                    border: "none",
                    background: "none",
                    color: "#6B6B66",
                    fontSize: 12.5,
                    padding: 0,
                  }}
                >
                  Use a different email
                </button>
              </div>
            </>
          )}

          <div
            style={{
              marginTop: 26,
              paddingTop: 16,
              borderTop: "1px solid #F0F0EC",
              textAlign: "center",
              fontSize: 11,
              fontFamily: "ui-monospace,'SF Mono',monospace",
              color: "#A6A6A0",
              lineHeight: 1.6,
            }}
          >
            {localLogin ? (
              "Email sign-in returns when the app is hosted."
            ) : (
              <>
                The 6-digit code arrives by email.
                <br />
                Without an email key it is printed on the server console.
              </>
            )}
          </div>
        </div>
      </div>

      <div
        style={{
          textAlign: "center",
          padding: 20,
          fontSize: 11.5,
          color: "#A6A6A0",
          position: "relative",
        }}
      >
        Talkstream preview · © 2026
      </div>
    </div>
  );
}
