'use client';

import {
  Bot,
  ChevronRight,
  Globe2,
  HardDrive,
  KeyRound,
  Laptop,
  Moon,
  Palette,
  ShieldCheck,
  SlidersHorizontal,
  Sun,
} from 'lucide-react';
import { useState } from 'react';
import type { RuntimeBootstrap } from '../lib/runtime-adapter';
import type { RuntimeStatus } from '../lib/pulsara-types';

type SettingsSection = 'general' | 'models' | 'service';

interface SettingsViewProps {
  theme: 'light' | 'dark';
  bootstrap?: RuntimeBootstrap;
  runtimeStatus: RuntimeStatus;
  onThemeChange: (theme: 'light' | 'dark') => void;
}

const navItems = [
  { id: 'general' as const, label: '通用', icon: SlidersHorizontal },
  { id: 'models' as const, label: '模型', icon: Bot },
  { id: 'service' as const, label: '本地服务', icon: HardDrive },
];

const statusLabels: Record<RuntimeStatus, string> = {
  starting: '正在连接',
  online: '连接正常',
  reconnecting: '正在重连',
  offline: '连接中断',
  failed: '连接失败',
};

function SettingRow({ icon: Icon, title, detail, children }: {
  icon: typeof Sun;
  title: string;
  detail: string;
  children: React.ReactNode;
}) {
  return <div className="setting-row"><span className="setting-row__icon"><Icon size={15} /></span><span className="setting-row__copy"><strong>{title}</strong><small>{detail}</small></span><div className="setting-row__control">{children}</div></div>;
}

export function SettingsView({
  theme,
  bootstrap,
  runtimeStatus,
  onThemeChange,
}: SettingsViewProps) {
  const [section, setSection] = useState<SettingsSection>('general');
  const provider = bootstrap?.provider;

  return (
    <section className="surface-view settings-view">
      <header className="page-header"><div><span className="page-kicker">本机设置</span><h1>设置</h1><p>调整实际生效的外观，并查看本机模型与服务。</p></div></header>

      <div className="settings-layout">
        <aside className="settings-nav">
          <div className="settings-profile"><span>PL</span><div><strong>本机用户</strong><small>无需账号登录</small></div></div>
          {navItems.map(({ id, label, icon: Icon }) => <button className={section === id ? 'is-active' : ''} key={id} onClick={() => setSection(id)}><Icon size={14} /><span>{label}</span><ChevronRight size={12} /></button>)}
          <div className="settings-version"><strong>Pulsara</strong><span>{bootstrap?.application.version ?? '本地版本'}</span><small>运行在这台 Mac 上</small></div>
        </aside>

        <div className="settings-content">
          {section === 'general' && (
            <section className="settings-group"><header><Palette size={16} /><div><h2>外观</h2><p>控制 Pulsara 在本机的呈现方式。</p></div></header>
              <SettingRow icon={theme === 'light' ? Sun : Moon} title="主题" detail="切换明暗外观">
                <div className="theme-picker"><button className={theme === 'light' ? 'is-active' : ''} onClick={() => onThemeChange('light')}><Sun size={12} /> 浅色</button><button className={theme === 'dark' ? 'is-active' : ''} onClick={() => onThemeChange('dark')}><Moon size={12} /> 深色</button></div>
              </SettingRow>
            </section>
          )}

          {section === 'models' && (
            <>
              <section className="settings-group"><header><Bot size={16} /><div><h2>当前模型</h2><p>模型由本机启动配置提供。</p></div></header>
                <SettingRow icon={Bot} title="主要模型" detail="复杂推理与长时工作"><code className="value-code">{provider?.pro_model || '未配置'}</code></SettingRow>
                <SettingRow icon={Bot} title="轻量模型" detail="分类与辅助任务"><code className="value-code">{provider?.flash_model || '未配置'}</code></SettingRow>
              </section>
              <section className="settings-group"><header><Globe2 size={16} /><div><h2>模型服务</h2><p>连接信息只保存在本机，不需要网站账号。</p></div></header>
                <SettingRow icon={Globe2} title="服务" detail={provider?.provider || '尚未读取'}><code className="value-code">{provider?.endpoint_origin || '未配置'}</code></SettingRow>
                <SettingRow icon={KeyRound} title="访问密钥" detail="仅由本地服务读取"><span className="healthy-value">{provider?.api_key_set ? <><i /> 已配置</> : '未配置'}</span></SettingRow>
                <div className="provider-check"><span><i /><strong>{runtimeStatus === 'online' ? '已就绪' : statusLabels[runtimeStatus]}</strong><small>配置修改后需重新启动 Pulsara</small></span></div>
              </section>
            </>
          )}

          {section === 'service' && (
            <section className="settings-group"><header><HardDrive size={16} /><div><h2>本地服务</h2><p>Pulsara 的任务和会话都在这台设备上运行。</p></div></header>
              <SettingRow icon={HardDrive} title="连接状态" detail="浏览器与本地服务"><span className={runtimeStatus === 'online' ? 'healthy-value' : ''}>{runtimeStatus === 'online' && <i />} {statusLabels[runtimeStatus]}</span></SettingRow>
              <SettingRow icon={Laptop} title="数据位置" detail="会话数据保存在本机"><span className="storage-value">本地</span></SettingRow>
              <SettingRow icon={ShieldCheck} title="登录方式" detail="仅允许本机访问"><span className="storage-value">无需账号</span></SettingRow>
            </section>
          )}
        </div>
      </div>
    </section>
  );
}
