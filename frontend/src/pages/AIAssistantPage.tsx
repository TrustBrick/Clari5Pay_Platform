import React, { useState, useRef, useEffect } from 'react';
import { T } from '../utils/theme';
import { Card, Btn } from '../components/UI';
import { aiAPI } from '../services/api';
import type { User, AIMessage } from '../types';

const SUGGESTED_PROMPTS = [
  'What does my transaction volume look like this week?',
  'Explain the difference between deposit types (UPI, IMPS, NEFT)',
  'What is my current risk score and how can I improve it?',
  'How does the 2-step approval workflow work?',
  'What fees apply to my withdrawals?',
];

interface AIAssistantPageProps {
  user: User;
}

const AIAssistantPage: React.FC<AIAssistantPageProps> = ({ user }) => {
  const [messages, setMessages] = useState<AIMessage[]>([
    {
      role: 'assistant',
      content: `Hello ${user.name.split(' ')[0]}! 👋 I'm your Clari5Pay AI Assistant powered by Claude. I can help you understand your transactions, explain platform features, analyze risks, and answer any questions about your payment operations. How can I help you today?`,
    },
  ]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const sendMessage = async (text?: string) => {
    const content = text || input.trim();
    if (!content || loading) return;

    const userMsg: AIMessage = { role: 'user', content };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInput('');
    setLoading(true);

    try {
      const res = await aiAPI.chat(newMessages);
      setMessages(prev => [...prev, { role: 'assistant', content: res.reply }]);
    } catch {
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: '⚠ Sorry, I encountered an error. Please try again.' },
      ]);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ maxWidth: 800, height: 'calc(100vh - 120px)', display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Header */}
      <Card style={{ padding: '16px 20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div style={{ width: 44, height: 44, borderRadius: 14, background: T.grad1, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 22 }}>🤖</div>
          <div>
            <h2 style={{ margin: 0, fontSize: 15, fontWeight: 800 }}>Clari5Pay AI Assistant</h2>
            <p style={{ margin: 0, fontSize: 12, color: T.textMuted }}>Powered by Anthropic Claude · Payment intelligence at your fingertips</p>
          </div>
          <div style={{ marginLeft: 'auto', display: 'flex', alignItems: 'center', gap: 6 }}>
            <div style={{ width: 8, height: 8, borderRadius: '50%', background: T.success }} />
            <span style={{ fontSize: 11, color: T.success, fontWeight: 700 }}>Online</span>
          </div>
        </div>
      </Card>

      {/* Messages */}
      <Card style={{ flex: 1, padding: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
        <div style={{ flex: 1, overflowY: 'auto', padding: 20, display: 'flex', flexDirection: 'column', gap: 16 }}>
          {messages.map((msg, i) => (
            <div key={i} style={{ display: 'flex', justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start', gap: 10 }}>
              {msg.role === 'assistant' && (
                <div style={{ width: 32, height: 32, borderRadius: 10, background: T.grad1, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16, flexShrink: 0, marginTop: 4 }}>🤖</div>
              )}
              <div style={{
                maxWidth: '75%', padding: '12px 16px', borderRadius: msg.role === 'user' ? '18px 18px 4px 18px' : '18px 18px 18px 4px',
                background: msg.role === 'user' ? T.grad1 : T.canvas,
                color: msg.role === 'user' ? '#fff' : T.textMain,
                fontSize: 13, lineHeight: 1.6, fontWeight: 400,
                boxShadow: '0 2px 8px rgba(0,0,0,0.06)',
              }}>
                {msg.content}
              </div>
              {msg.role === 'user' && (
                <div style={{ width: 32, height: 32, borderRadius: 10, background: T.canvas, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 14, fontWeight: 700, color: T.blue, flexShrink: 0, marginTop: 4, border: `1px solid ${T.border}` }}>
                  {user.name.charAt(0)}
                </div>
              )}
            </div>
          ))}
          {loading && (
            <div style={{ display: 'flex', justifyContent: 'flex-start', gap: 10 }}>
              <div style={{ width: 32, height: 32, borderRadius: 10, background: T.grad1, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 16, flexShrink: 0 }}>🤖</div>
              <div style={{ padding: '12px 16px', borderRadius: '18px 18px 18px 4px', background: T.canvas, display: 'flex', gap: 6, alignItems: 'center' }}>
                {[0, 1, 2].map(i => (
                  <div key={i} style={{ width: 8, height: 8, borderRadius: '50%', background: T.blue, opacity: 0.6, animation: `pulse ${0.8 + i * 0.2}s ease infinite alternate` }} />
                ))}
              </div>
            </div>
          )}
          <div ref={bottomRef} />
        </div>

        {/* Suggested Prompts */}
        {messages.length <= 1 && (
          <div style={{ padding: '0 20px 12px', display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {SUGGESTED_PROMPTS.map((p, i) => (
              <button key={i} onClick={() => sendMessage(p)}
                style={{ padding: '6px 12px', borderRadius: 20, background: T.infoBg, border: `1px solid ${T.blue}30`, color: T.blue, fontSize: 11, fontWeight: 600, cursor: 'pointer', fontFamily: 'inherit', transition: 'all 0.15s' }}
                onMouseEnter={e => { (e.currentTarget as HTMLButtonElement).style.background = T.blue; (e.currentTarget as HTMLButtonElement).style.color = '#fff'; }}
                onMouseLeave={e => { (e.currentTarget as HTMLButtonElement).style.background = T.infoBg; (e.currentTarget as HTMLButtonElement).style.color = T.blue; }}>
                {p}
              </button>
            ))}
          </div>
        )}

        {/* Input */}
        <div style={{ padding: '12px 16px', borderTop: `1px solid ${T.border}`, display: 'flex', gap: 10 }}>
          <input
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && !e.shiftKey && sendMessage()}
            placeholder="Ask me about transactions, fees, risk analysis, approvals..."
            style={{ flex: 1, padding: '10px 14px', border: `1.5px solid ${T.border}`, borderRadius: 12, fontSize: 13, outline: 'none', fontFamily: 'inherit', color: T.textMain, background: T.canvas }}
            onFocus={e => { e.target.style.borderColor = T.blue; e.target.style.background = T.surface; }}
            onBlur={e => { e.target.style.borderColor = T.border; e.target.style.background = T.canvas; }}
          />
          <Btn onClick={() => sendMessage()} disabled={!input.trim() || loading} style={{ borderRadius: 12 }}>
            {loading ? '...' : '→ Send'}
          </Btn>
        </div>
      </Card>
    </div>
  );
};

export default AIAssistantPage;
