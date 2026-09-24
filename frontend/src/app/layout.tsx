import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "My Booth Agent — हर बूथ की समझ",
  description: "Booth-level electoral intelligence. Har booth ki samajh.",
  icons: { icon: "/logo.png" },
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body className="min-h-screen antialiased">{children}</body>
    </html>
  );
}
