import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: "Job Match",
  description: "Review matches and prepare grounded applications.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>
        <header className="siteHeader">
          <Link className="brand" href="/">
            <span className="brandMark" aria-hidden="true">
              JM
            </span>
            <span>Job Match</span>
          </Link>
          <nav className="siteNav" aria-label="Primary navigation">
            <Link href="/">Matches</Link>
            <Link href="/profile">Profile</Link>
            <Link href="/admin">Admin</Link>
          </nav>
          <span className="localBadge">Local PoC</span>
        </header>
        {children}
      </body>
    </html>
  );
}
