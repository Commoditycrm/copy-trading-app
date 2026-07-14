/**
 * Public Terms of Service — sits OUTSIDE the (app) auth-gated route group so
 * anonymous visitors (SnapTrade reviewers, prospective users) can reach it
 * without a login. Static server component.
 *
 * Plain-language starting point reflecting what the app does (accounts,
 * SnapTrade broker connections, trade mirroring). Have it reviewed by counsel
 * before relying on it — it is not legal advice.
 */
import Link from "next/link";

export const metadata = {
  title: "Terms of Service — Kopyya",
  description: "The terms that govern your use of Kopyya.",
};

const UPDATED = "July 2026";
const SUPPORT_EMAIL = "support@kopyya.com";

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="mt-8">
      <h2 className="text-lg font-semibold mb-2" style={{ color: "var(--text)" }}>{title}</h2>
      <div className="space-y-3 text-sm leading-relaxed" style={{ color: "var(--text-2)" }}>
        {children}
      </div>
    </section>
  );
}

function Bullets({ items }: { items: string[] }) {
  return (
    <ul className="list-disc pl-5 space-y-1.5">
      {items.map((t, i) => <li key={i}>{t}</li>)}
    </ul>
  );
}

export default function TermsPage() {
  return (
    <main className="relative min-h-screen" style={{ background: "var(--bg)", color: "var(--text)" }}>
      <header className="px-6 sm:px-10 py-6 flex items-center justify-between border-b" style={{ borderColor: "var(--border)" }}>
        <Link href="/" className="text-base font-semibold tracking-tight no-underline" style={{ color: "var(--text)" }}>
          Kopyya
        </Link>
        <Link href="/" className="text-sm no-underline" style={{ color: "var(--muted)" }}>
          ← Back to home
        </Link>
      </header>

      <div className="mx-auto max-w-3xl px-6 sm:px-8 py-10">
        <h1 className="text-3xl font-bold tracking-tight">Terms of Service</h1>
        <p className="mt-2 text-sm" style={{ color: "var(--muted)" }}>Last updated: {UPDATED}</p>

        <p className="mt-6 text-sm leading-relaxed" style={{ color: "var(--text-2)" }}>
          These Terms of Service (&quot;Terms&quot;) govern your access to and use of kopyya.com and the Kopyya
          application (the &quot;Service&quot;), operated by Kopyya (&quot;Kopyya&quot;, &quot;we&quot;, &quot;us&quot;).
          By creating an account or using the Service, you agree to these Terms. If you do not agree, do not use the
          Service.
        </p>

        <Section title="1. Eligibility">
          <p>
            You must be at least 18 years old and able to form a binding contract to use the Service. By using it,
            you represent that you meet these requirements and that the information you provide is accurate.
          </p>
        </Section>

        <Section title="2. What Kopyya is (and is not)">
          <p>
            Kopyya is a technology platform that lets a subscriber automatically mirror a trader&apos;s orders in the
            subscriber&apos;s own connected brokerage account. Kopyya is <strong>not</strong> a broker-dealer,
            investment adviser, or custodian. We do not hold your funds or securities — those remain in your own
            brokerage account. Brokerage connectivity is provided through SnapTrade, and orders are executed by your
            brokerage, not by Kopyya.
          </p>
        </Section>

        <Section title="3. Not investment advice">
          <p>
            The Service does not provide investment, financial, tax, or legal advice, and nothing on it is a
            recommendation to buy or sell any security or to adopt any strategy. Choosing which trader to follow and
            configuring your copy settings are your decisions alone. You are solely responsible for your trading
            activity and its outcomes.
          </p>
        </Section>

        <Section title="4. Trading risk">
          <Bullets items={[
            "Trading involves substantial risk, including the possible loss of your entire investment. Options and short-dated contracts can be especially volatile.",
            "Past performance of any trader is not indicative of future results.",
            "You may lose money copying a trader, and you accept full responsibility for that risk.",
          ]} />
        </Section>

        <Section title="5. No guarantee of execution or mirroring">
          <p>
            We work to mirror trades accurately and promptly, but we cannot guarantee it. Orders may be delayed,
            partially filled, rejected, cancelled, or not placed at all due to market conditions, price movement,
            brokerage or SnapTrade behavior, connectivity, or other factors. As a result, your fills, prices,
            timing, and positions may differ from the trader&apos;s, and your account may at times be out of sync
            with the trader you follow. You are responsible for monitoring your own account.
          </p>
        </Section>

        <Section title="6. Your account">
          <Bullets items={[
            "Keep your login credentials confidential; you are responsible for activity under your account.",
            "Provide accurate information and keep it current.",
            "Notify us promptly of any unauthorized use or security concern at " + SUPPORT_EMAIL + ".",
          ]} />
        </Section>

        <Section title="7. Brokerage connections">
          <p>
            By connecting a brokerage through SnapTrade, you authorize Kopyya to place, modify, cancel, and read
            orders and positions in that account according to your settings. You remain the account holder and are
            responsible for your brokerage relationship, eligibility, margin, and any brokerage fees. You can
            disconnect a brokerage at any time from the Broker page, which stops further order activity from Kopyya.
          </p>
        </Section>

        <Section title="8. Acceptable use">
          <p>You agree not to:</p>
          <Bullets items={[
            "Use the Service for any unlawful purpose or in violation of your brokerage's or SnapTrade's terms.",
            "Interfere with, disrupt, probe, or attempt to gain unauthorized access to the Service or its infrastructure.",
            "Reverse engineer, scrape, or resell the Service except as permitted by law.",
            "Misrepresent your identity or impersonate others.",
          ]} />
        </Section>

        <Section title="9. Third-party services">
          <p>
            The Service relies on third parties including SnapTrade, your brokerage, and messaging providers. Your
            use of those services is also governed by their terms and policies, and we are not responsible for their
            acts or omissions. See our{" "}
            <Link href="/privacy" className="underline" style={{ color: "var(--accent)" }}>Privacy Policy</Link>{" "}
            for how data is handled.
          </p>
        </Section>

        <Section title="10. Fees">
          <p>
            Any fees for the Service will be disclosed to you before they apply. Brokerage commissions and fees are
            charged by your brokerage and are your responsibility.
          </p>
        </Section>

        <Section title="11. Disclaimers">
          <p>
            The Service is provided &quot;as is&quot; and &quot;as available,&quot; without warranties of any kind,
            express or implied, including merchantability, fitness for a particular purpose, and non-infringement. We
            do not warrant that the Service will be uninterrupted, error-free, or that trades will be executed as
            intended.
          </p>
        </Section>

        <Section title="12. Limitation of liability">
          <p>
            To the maximum extent permitted by law, Kopyya and its operators will not be liable for any indirect,
            incidental, special, consequential, or punitive damages, or for any trading losses, lost profits, or
            loss of data, arising out of or relating to your use of the Service. Our total liability for any claim
            relating to the Service will not exceed the greater of the amounts you paid us in the twelve months
            before the claim or USD 100.
          </p>
        </Section>

        <Section title="13. Indemnification">
          <p>
            You agree to indemnify and hold harmless Kopyya and its operators from any claims, losses, and expenses
            arising out of your use of the Service, your trading activity, or your violation of these Terms.
          </p>
        </Section>

        <Section title="14. Termination">
          <p>
            You may stop using the Service and close your account at any time. We may suspend or terminate access if
            you violate these Terms or to protect the Service or its users. Provisions that by their nature should
            survive termination (such as disclaimers, limitation of liability, and indemnification) will survive.
          </p>
        </Section>

        <Section title="15. Changes to these Terms">
          <p>
            We may update these Terms from time to time. When we do, we will revise the &quot;Last updated&quot;
            date above and, for material changes, provide a more prominent notice. Continued use after changes take
            effect means you accept the updated Terms.
          </p>
        </Section>

        <Section title="16. Contact">
          <p>
            Questions about these Terms? Email us at{" "}
            <a href={`mailto:${SUPPORT_EMAIL}`} style={{ color: "var(--accent)" }}>{SUPPORT_EMAIL}</a>.
          </p>
        </Section>

        <div className="mt-10 pt-6 border-t text-xs" style={{ borderColor: "var(--border)", color: "var(--muted)" }}>
          © {new Date().getFullYear()} Kopyya. All rights reserved.
        </div>
      </div>
    </main>
  );
}
