import type { Metadata } from 'next';
import 'katex/dist/katex.min.css';
import './globals.css';
import './styles/base.css';
import './styles/shell.css';
import './styles/workbench.css';
import './styles/inspector.css';
import './styles/overview.css';
import './styles/settings.css';
import './styles/capabilities.css';
import './styles/overlays.css';
import './styles/responsive.css';

export const metadata: Metadata = {
  metadataBase: new URL('https://pulsara-observatory.vibewithplum.chatgpt.site'),
  title: 'Pulsara — 本地智能工作台',
  description:
    '无需账号登录的本地 Agent 工作台。',
  applicationName: 'Pulsara',
  alternates: {
    canonical: '/',
  },
  openGraph: {
    type: 'website',
    url: '/',
    siteName: 'Pulsara',
    title: 'Pulsara — 本地智能工作台',
    description: '持久会话、可见执行过程与逐轮控制。',
    images: [
      {
        url: '/og.png',
        width: 2244,
        height: 701,
        alt: 'Pulsara 本地智能工作台',
      },
    ],
  },
  twitter: {
    card: 'summary_large_image',
    title: 'Pulsara — 本地智能工作台',
    description: '持久会话、可见执行过程与逐轮控制。',
    images: ['/og.png'],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
