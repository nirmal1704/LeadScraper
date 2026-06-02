import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'LeadScraper',
  description: 'Find small local businesses without websites.',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
