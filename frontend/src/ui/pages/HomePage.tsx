import { useEffect, useState } from "react";

import { HeroParticles } from "../components/HeroParticles";

type HomePageProps = {
  onLogin: () => void;
};

const HERO_WORDS = ["listens", "answers", "remembers"];

// Same wave as the login hero, for cross-page uniformity.
const HERO_WAVE = Array.from({ length: 36 }, (_, i) => ({
  color: `oklch(0.62 0.17 ${(262 - i * 2.6).toFixed(0)})`,
  delay: `${(-(i * 0.11)).toFixed(2)}s`,
}));

const GRID_H = Array.from({ length: 8 }, (_, i) => `${12.5 * (i + 1)}%`);
const GRID_V = Array.from({ length: 12 }, (_, i) => `${(8.33 * (i + 1)).toFixed(2)}%`);
const FEAT_WAVE = Array.from({ length: 18 }, (_, i) => ({
  delay: `${((i % 6) * 0.12).toFixed(2)}s`,
  opacity: Number((0.35 + 0.6 * Math.abs(Math.sin(i * 1.7))).toFixed(2)),
}));

const SERIF = "'Instrument Serif',Georgia,serif";
const MONO = "'JetBrains Mono',ui-monospace,monospace";

function Logo() {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
      <div
        style={{
          width: 22,
          height: 22,
          borderRadius: 7,
          background: "#111110",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          gap: 2,
        }}
      >
        <span style={{ width: 2, height: 7, borderRadius: 1, background: "#FFFFFF" }} />
        <span style={{ width: 2, height: 11, borderRadius: 1, background: "#FFFFFF" }} />
        <span style={{ width: 2, height: 5, borderRadius: 1, background: "#FFFFFF" }} />
      </div>
      <div style={{ fontSize: 15, fontWeight: 600, letterSpacing: "-0.01em" }}>
        Talkstream<span style={{ color: "#4A6CF7" }}>AI</span>
      </div>
    </div>
  );
}

function SectionKicker({ label }: { label: string }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        fontFamily: MONO,
        fontSize: 13,
        color: "#6B6B66",
      }}
    >
      <span style={{ width: 32, height: 1, background: "rgba(17,17,16,0.3)" }} />
      <span>{label}</span>
    </div>
  );
}

function LoginButton({ onLogin, large }: { onLogin: () => void; large?: boolean }) {
  return (
    <button
      type="button"
      onClick={onLogin}
      className="hovDark"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 10,
        border: "none",
        borderRadius: 999,
        background: "#111110",
        color: "#FFFFFF",
        fontSize: large ? 15.5 : 13.5,
        fontWeight: 500,
        padding: large ? "16px 32px" : "9px 22px",
      }}
    >
      Log in {large && <span aria-hidden="true">→</span>}
    </button>
  );
}

const FEATURES: Array<{ index: string; title: string; body: string }> = [
  {
    index: "01",
    title: "Speaks your language",
    body: "Speak in English, Hindi, or Hinglish. The assistant follows your language as the conversation moves.",
  },
  {
    index: "02",
    title: "Gets things done",
    body: "Ask questions, add todos, and search the live web. Todo changes appear in your list during the call.",
  },
  {
    index: "03",
    title: "Metered demo calls",
    body: "Calls use demo wallet credit by the second. Add credit and review each session on the ledger.",
  },
  {
    index: "04",
    title: "Safe by default",
    body: "PII redaction, blocked topics, spend caps and prompt-injection checks — every guardrail on from the first call.",
  },
];

const STEPS: Array<{ index: string; title: string; body: string }> = [
  {
    index: "01",
    title: "Sign in",
    body: "One email, one 6-digit code. No passwords, no forms.",
  },
  {
    index: "02",
    title: "Press call",
    body: "Allow microphone access and start speaking in Hindi, English, or both.",
  },
  {
    index: "03",
    title: "It gets done",
    body: "Answers, todos, and web searches happen live. Demo wallet credit is metered by the second.",
  },
];

function FeatureGlyph({ index }: { index: string }) {
  if (index === "01") {
    return (
      <div style={{ display: "flex", alignItems: "center", gap: 4, height: 80 }}>
        {FEAT_WAVE.map((bar, i) => (
          <div
            key={i}
            style={{
              width: 4,
              height: 64,
              borderRadius: 2,
              background: "#111110",
              transformOrigin: "center",
              animation: "waveSpeak 1.3s ease-in-out infinite",
              animationDelay: bar.delay,
              opacity: bar.opacity,
            }}
          />
        ))}
      </div>
    );
  }
  if (index === "02") {
    const satellites: Array<[number, number, number]> = [
      [122, 67, 0],
      [95, 115, 0.3],
      [40, 115, 0.6],
      [12, 67, 0.9],
      [40, 19, 1.2],
      [95, 19, 1.5],
    ];
    return (
      <div style={{ position: "relative", width: 150, height: 150 }}>
        <div
          style={{
            position: "absolute",
            left: 65,
            top: 65,
            width: 20,
            height: 20,
            borderRadius: "50%",
            background: "#111110",
            animation: "orbBreathe 2s ease-in-out infinite",
          }}
        />
        <div
          style={{
            position: "absolute",
            left: 55,
            top: 55,
            width: 40,
            height: 40,
            border: "1px solid rgba(17,17,16,0.35)",
            borderRadius: "50%",
            animation: "ringGrow 2s ease-out infinite",
          }}
        />
        {satellites.map(([left, top, delay]) => (
          <div
            key={`${left}-${top}`}
            style={{
              position: "absolute",
              left,
              top,
              width: 12,
              height: 12,
              border: "2px solid #111110",
              borderRadius: "50%",
              animation: "blinkDot 2s infinite",
              animationDelay: `${delay}s`,
            }}
          />
        ))}
      </div>
    );
  }
  if (index === "03") {
    return (
      <div style={{ display: "flex", alignItems: "center", width: 220 }}>
        <div
          style={{
            flexShrink: 0,
            width: 56,
            height: 64,
            border: "2px solid #111110",
            borderRadius: 4,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontFamily: MONO,
            fontSize: 22,
          }}
        >
          ₹
        </div>
        <div
          style={{
            flex: 1,
            position: "relative",
            height: 0,
            borderTop: "2px dashed rgba(17,17,16,0.4)",
            margin: "0 -2px",
          }}
        >
          <div
            style={{
              position: "absolute",
              top: -5,
              left: 0,
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: "#111110",
              animation: "packetMove 1.6s linear infinite",
            }}
          />
        </div>
        <div
          style={{
            flexShrink: 0,
            width: 56,
            height: 64,
            border: "2px solid #111110",
            borderRadius: 4,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            gap: 3,
          }}
        >
          <span style={{ width: 3, height: 12, borderRadius: 1.5, background: "#111110" }} />
          <span style={{ width: 3, height: 20, borderRadius: 1.5, background: "#111110" }} />
          <span style={{ width: 3, height: 9, borderRadius: 1.5, background: "#111110" }} />
        </div>
      </div>
    );
  }
  return (
    <div
      style={{
        position: "relative",
        width: 150,
        height: 150,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
      }}
    >
      <div
        style={{
          position: "absolute",
          inset: 25,
          border: "1px solid rgba(17,17,16,0.3)",
          borderRadius: "50%",
          animation: "ringGrow 2.4s ease-out infinite",
        }}
      />
      <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
        <div
          style={{
            width: 34,
            height: 22,
            border: "4px solid #111110",
            borderBottom: "none",
            borderRadius: "17px 17px 0 0",
            boxSizing: "border-box",
          }}
        />
        <div
          style={{
            width: 56,
            height: 42,
            background: "#111110",
            borderRadius: 6,
            marginTop: -2,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        >
          <div style={{ width: 8, height: 14, background: "#F7F7F5", borderRadius: 4 }} />
        </div>
      </div>
    </div>
  );
}

export function HomePage({ onLogin }: HomePageProps) {
  const [wordIndex, setWordIndex] = useState(0);

  useEffect(() => {
    const timer = window.setInterval(
      () => setWordIndex((current) => (current + 1) % HERO_WORDS.length),
      2500,
    );
    return () => window.clearInterval(timer);
  }, []);

  return (
    <div className="home-page" style={{ minHeight: "100vh", background: "#F7F7F5", color: "#111110" }}>
      <header
        style={{
          position: "fixed",
          top: 0,
          left: 0,
          right: 0,
          zIndex: 60,
          background: "rgba(247,247,245,0.82)",
          backdropFilter: "blur(16px)",
          WebkitBackdropFilter: "blur(16px)",
          borderBottom: "1px solid rgba(17,17,16,0.07)",
        }}
      >
        <div className="home-header-inner"
          style={{
            maxWidth: 1400,
            margin: "0 auto",
            height: 72,
            padding: "0 32px",
            boxSizing: "border-box",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: 24,
          }}
        >
          <Logo />
          <nav className="home-header-nav" style={{ display: "flex", alignItems: "center", gap: 40 }}>
            <a
              href="#features"
              className="hovNavLink"
              style={{ fontSize: 13.5, color: "#6B6B66", textDecoration: "none" }}
            >
              Features
            </a>
            <a
              href="#how"
              className="hovNavLink"
              style={{ fontSize: 13.5, color: "#6B6B66", textDecoration: "none" }}
            >
              How it works
            </a>
          </nav>
          <LoginButton onLogin={onLogin} />
        </div>
      </header>

      <section className="home-hero"
        style={{
          position: "relative",
          minHeight: "100vh",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          overflow: "hidden",
          boxSizing: "border-box",
        }}
      >
        <div style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
          {GRID_H.map((pos) => (
            <div
              key={`h-${pos}`}
              style={{
                position: "absolute",
                left: 0,
                right: 0,
                height: 1,
                background: "rgba(17,17,16,0.05)",
                top: pos,
              }}
            />
          ))}
          {GRID_V.map((pos) => (
            <div
              key={`v-${pos}`}
              style={{
                position: "absolute",
                top: 0,
                bottom: 0,
                width: 1,
                background: "rgba(17,17,16,0.05)",
                left: pos,
              }}
            />
          ))}
        </div>
        <HeroParticles />
        <div className="home-hero-inner"
          style={{
            position: "relative",
            zIndex: 1,
            maxWidth: 1400,
            margin: "0 auto",
            width: "100%",
            boxSizing: "border-box",
            padding: "150px 48px 100px",
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 4,
              height: 40,
              marginBottom: 26,
              animation: "fadeUp .6s ease both",
            }}
          >
            {HERO_WAVE.map((bar, index) => (
              <div
                key={index}
                style={{
                  width: 4,
                  height: 36,
                  borderRadius: 2,
                  background: bar.color,
                  transformOrigin: "center",
                  animation: "waveListen 2.6s ease-in-out infinite",
                  animationDelay: bar.delay,
                }}
              />
            ))}
          </div>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 12,
              fontFamily: MONO,
              fontSize: 13,
              color: "#6B6B66",
              animation: "fadeUp .7s ease both",
            }}
          >
            <span style={{ width: 32, height: 1, background: "rgba(17,17,16,0.3)" }} />
            <span>Voice assistant preview · English, Hindi, and Hinglish.</span>
          </div>
          <h1
            style={{
              margin: "28px 0 0",
              fontFamily: SERIF,
              fontWeight: 400,
              fontSize: "clamp(56px,10vw,148px)",
              lineHeight: 0.95,
              letterSpacing: "-0.02em",
              animation: "fadeUp .9s ease both",
            }}
          >
            <span style={{ display: "block" }}>An assistant</span>
            <span style={{ display: "block" }}>
              that{" "}
              <span
                style={{
                  display: "inline-block",
                  minWidth: "8ch",
                  textAlign: "left",
                  color: "#4A6CF7",
                }}
              >
                <span
                  key={wordIndex}
                  style={{
                    display: "inline-block",
                    animation: "wordIn .5s cubic-bezier(0.22,1,0.36,1) both",
                  }}
                >
                  {HERO_WORDS[wordIndex]}
                </span>
              </span>
            </span>
          </h1>
          <div className="home-hero-lower"
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit,minmax(340px,1fr))",
              gap: 48,
              alignItems: "end",
              marginTop: 64,
            }}
          >
            <p
              style={{
                margin: 0,
                fontSize: 21,
                lineHeight: 1.6,
                color: "#6B6B66",
                maxWidth: 540,
                textWrap: "pretty",
                animation: "fadeUp 1s ease both",
              }}
            >
              Talkstream answers questions, manages your todos, and searches the live web — all
              by voice, in English, Hindi, or Hinglish. Sign in, press call, bas boliye.
            </p>
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: 16,
                animation: "fadeUp 1.1s ease both",
              }}
            >
              <LoginButton onLogin={onLogin} large />
              <a
                href="#how"
                className="hovOutline"
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 8,
                  border: "1px solid rgba(17,17,16,0.2)",
                  borderRadius: 999,
                  color: "#111110",
                  fontFamily: MONO,
                  fontSize: 13,
                  padding: "14px 24px",
                  textDecoration: "none",
                }}
              >
                See how it works ↓
              </a>
            </div>
          </div>
        </div>
      </section>

      <section id="features" style={{ padding: "110px 0 60px", scrollMarginTop: 80 }}>
        <div className="home-section-inner"
          style={{ maxWidth: 1400, margin: "0 auto", padding: "0 48px", boxSizing: "border-box" }}
        >
          <SectionKicker label="Capabilities" />
          <h2
            style={{
              margin: "24px 0 0",
              fontFamily: SERIF,
              fontWeight: 400,
              fontSize: "clamp(40px,5vw,64px)",
              lineHeight: 1.05,
              letterSpacing: "-0.02em",
            }}
          >
            Help through conversation.
            <br />
            <span style={{ color: "#6B6B66" }}>Actions you can see.</span>
          </h2>

          <div style={{ marginTop: 56, borderTop: "1px solid rgba(17,17,16,0.1)" }}>
            {FEATURES.map((feature) => (
              <div
                key={feature.index}
                style={{
                  display: "flex",
                  gap: 56,
                  padding: "64px 0",
                  borderBottom: "1px solid rgba(17,17,16,0.1)",
                  alignItems: "center",
                  flexWrap: "wrap",
                }}
              >
                <div
                  style={{
                    flexShrink: 0,
                    width: 40,
                    fontFamily: MONO,
                    fontSize: 13,
                    color: "#6B6B66",
                  }}
                >
                  {feature.index}
                </div>
                <div style={{ flex: 1, minWidth: 280 }}>
                  <h3
                    style={{
                      margin: 0,
                      fontFamily: SERIF,
                      fontWeight: 400,
                      fontSize: 36,
                      letterSpacing: "-0.01em",
                    }}
                  >
                    {feature.title}
                  </h3>
                  <p
                    style={{
                      margin: "16px 0 0",
                      fontSize: 17,
                      lineHeight: 1.65,
                      color: "#6B6B66",
                      maxWidth: 480,
                      textWrap: "pretty",
                    }}
                  >
                    {feature.body}
                  </p>
                </div>
                <div
                  style={{
                    flexShrink: 0,
                    width: 220,
                    height: 150,
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}
                >
                  <FeatureGlyph index={feature.index} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section id="how" style={{ padding: "60px 0 110px", scrollMarginTop: 80 }}>
        <div className="home-section-inner"
          style={{ maxWidth: 1400, margin: "0 auto", padding: "0 48px", boxSizing: "border-box" }}
        >
          <SectionKicker label="How it works" />
          <h2
            style={{
              margin: "24px 0 0",
              fontFamily: SERIF,
              fontWeight: 400,
              fontSize: "clamp(40px,5vw,64px)",
              lineHeight: 1.05,
              letterSpacing: "-0.02em",
            }}
          >
            Three steps. <span style={{ color: "#6B6B66" }}>Then just talk.</span>
          </h2>
          <div className="home-steps-grid"
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit,minmax(260px,1fr))",
              gap: 1,
              background: "rgba(17,17,16,0.1)",
              border: "1px solid rgba(17,17,16,0.1)",
              marginTop: 56,
            }}
          >
            {STEPS.map((step) => (
              <div
                key={step.index}
                style={{ background: "#F7F7F5", padding: "44px 36px", boxSizing: "border-box" }}
              >
                <div style={{ fontFamily: MONO, fontSize: 13, color: "#6B6B66" }}>
                  {step.index}
                </div>
                <h3
                  style={{
                    margin: "22px 0 0",
                    fontFamily: SERIF,
                    fontWeight: 400,
                    fontSize: 28,
                  }}
                >
                  {step.title}
                </h3>
                <p
                  style={{
                    margin: "12px 0 0",
                    fontSize: 15.5,
                    lineHeight: 1.65,
                    color: "#6B6B66",
                    textWrap: "pretty",
                  }}
                >
                  {step.body}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>

      <section style={{ padding: "0 0 120px" }}>
        <div className="home-section-inner"
          style={{ maxWidth: 1400, margin: "0 auto", padding: "0 48px", boxSizing: "border-box" }}
        >
          <div className="home-final-cta"
            style={{
              position: "relative",
              border: "1px solid #111110",
              padding: "96px 64px",
              boxSizing: "border-box",
            }}
          >
            <div
              style={{
                position: "absolute",
                top: 0,
                right: 0,
                width: 120,
                height: 120,
                borderLeft: "1px solid rgba(17,17,16,0.1)",
                borderBottom: "1px solid rgba(17,17,16,0.1)",
              }}
            />
            <div
              style={{
                position: "absolute",
                bottom: 0,
                left: 0,
                width: 120,
                height: 120,
                borderRight: "1px solid rgba(17,17,16,0.1)",
                borderTop: "1px solid rgba(17,17,16,0.1)",
              }}
            />
            <h2
              style={{
                margin: 0,
                fontFamily: SERIF,
                fontWeight: 400,
                fontSize: "clamp(44px,6vw,84px)",
                lineHeight: 0.98,
                letterSpacing: "-0.02em",
              }}
            >
              Ready when you are.
              <br />
              <span style={{ color: "#6B6B66" }}>Bas boliye.</span>
            </h2>
            <p
              style={{
                margin: "24px 0 0",
                fontSize: 18,
                lineHeight: 1.6,
                color: "#6B6B66",
                maxWidth: 520,
                textWrap: "pretty",
              }}
            >
              Sign in with your email, then start a voice call when you're ready.
            </p>
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: 16,
                marginTop: 40,
              }}
            >
              <LoginButton onLogin={onLogin} large />
            </div>
            <p
              style={{
                margin: "32px 0 0",
                fontFamily: MONO,
                fontSize: 12,
                color: "#A6A6A0",
              }}
            >
              OTP sign-in — the 6-digit code arrives by email.
            </p>
          </div>
        </div>
      </section>

      <footer style={{ borderTop: "1px solid rgba(17,17,16,0.1)" }}>
        <div className="home-footer-inner"
          style={{
            maxWidth: 1400,
            margin: "0 auto",
            padding: "64px 48px 48px",
            boxSizing: "border-box",
          }}
        >
          <Logo />
          <p
            style={{
              margin: "20px 0 0",
              fontSize: 14.5,
              lineHeight: 1.65,
              color: "#6B6B66",
              maxWidth: 320,
              textWrap: "pretty",
            }}
          >
            Ask questions, manage todos, search the web — all by voice.
          </p>
        </div>
        <div className="home-footer-inner"
          style={{
            maxWidth: 1400,
            margin: "0 auto",
            padding: "24px 48px",
            boxSizing: "border-box",
            borderTop: "1px solid rgba(17,17,16,0.1)",
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            gap: 16,
            flexWrap: "wrap",
            fontSize: 13,
            color: "#A6A6A0",
          }}
        >
          <span>© 2026 Talkstream. All rights reserved.</span>
          <span style={{ fontFamily: MONO, fontSize: 12 }}>English · Hindi · Hinglish preview</span>
        </div>
      </footer>
    </div>
  );
}
