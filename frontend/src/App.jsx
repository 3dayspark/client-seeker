import React, { useState, useEffect, useRef } from 'react';
import './App.css';

// ==========================================
// 1. 各種定数と画像のカスタマイズ設定
// ==========================================
const AI_ASSISTANT_NAME = "産業リサーチAI";
const USER_NAME = "自分";

// 【カスタマイズ: アバター画像】
// 独自の画像（jpg, png, svgなど）を使用する場合は、URLを置き換えるか、
// import文でローカル画像を読み込んで指定してください（例: import aiAvatar from './assets/ai-avatar.png'）
const AI_AVATAR = `https://img.icons8.com/?size=100&id=XY6XVIE2E3ig&format=png&color=007bff`;
const USER_AVATAR = `https://img.icons8.com/?size=100&id=6pZID8Z263Q9&format=png&color=007bff`;
const THINKING_ICON = `https://img.icons8.com/?size=100&id=yi4Wa8EFkMvI&format=png&color=888888`;

// 【カスタマイズ: 機能紹介アイコン】
// ウェルカム画面の3つの機能アイコン用画像URL
const ICON_PROFILE = `https://img.icons8.com/?size=100&id=44859&format=png&color=007bff`; // 顧客プロファイリング用
const ICON_SEARCH = `https://img.icons8.com/?size=100&id=44045&format=png&color=007bff`;    // スクリーニング用
const ICON_ANALYZE = `https://img.icons8.com/?size=100&id=48296&format=png&color=007bff`; // データ分析用

const SYSTEM_NOTICE = "AIバックエンドサービスは、日本時間の平日7:30〜21:30のみ稼働しております。現在、AI処理はt3.small環境上で動作しているため、応答に時間がかかる場合があります。ご不便をおかけいたしますが、何卒ご理解のほどよろしくお願いいたします。";

const QUICK_PROMPTS =[
  { label: "強化ガラスの顧客開拓", text: "当社は高品質な強化ガラスを製造しており、取引先となる企業を探しています。現在は主に自動車業界の顧客に注力しています。" },
  { label: "人型ロボットの顧客開拓", text: "当社はヒューマノイドロボットを製造しています。現在は主に物流業界の顧客に注力しています。" },
  { label: "商談中案件の金額確認", text: "現在進行中の商談先をすべて教えてください。それぞれの商談金額もあわせて教えてください。" }
];

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://localhost:8000';
const API_ENDPOINT = `${API_BASE_URL}/chat`;

// --- SVG アイコンコンポーネント ---
const RocketIcon = () => (<svg width="20" height="20" viewBox="0 0 24 24" fill="none" className="status-svg"><path d="M21 3C21 3 17.5 9.5 15.5 11.5C14.5 12.5 13.5 12.5 13.5 12.5L16 17.5L12 16L10.5 19L8.5 14L3 12.5L8 10.5C8 10.5 8 9.5 9 8.5C11 6.5 21 3 21 3Z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /><circle cx="14.5" cy="9.5" r="1.5" stroke="currentColor" strokeWidth="1.5" /><path d="M6.5 16.5L4 19" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/><path d="M10 18L8.5 20.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/><path d="M4.5 13.5L3 15" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>);
const CheckIcon = () => (<svg width="20" height="20" viewBox="0 0 24 24" fill="none" className="status-svg"><path d="M12 22C17.5 22 22 17.5 22 12C22 6.5 17.5 2 12 2C6.5 2 2 6.5 2 12C2 17.5 6.5 22 12 22Z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/><path d="M7.75 12L10.58 14.83L16.25 9.17004" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>);
const MenuIcon = () => (<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><line x1="3" y1="12" x2="21" y2="12"></line><line x1="3" y1="6" x2="21" y2="6"></line><line x1="3" y1="18" x2="21" y2="18"></line></svg>);

// --- LogContent コンポーネント ---
const LogContent = ({ logs, logRef, onImageClick }) => {
  const [isOpen, setIsOpen] = useState(true);
  const [currentPhase, setCurrentPhase] = useState("準備完了、開始待機中...");
  const [progress, setProgress] = useState(0);
  const [isCompleted, setIsCompleted] = useState(false);

  // フェーズと進捗率のマッピング
  const PHASE_MAP = {
    '1': 10, '1.5': 25, '2': 40, '3': 55, '3.5': 65, '4': 75, '4.5': 90, '5': 98
  };

  // ログの変化を監視し、プログレスバーと現在のフェーズを更新
  useEffect(() => {
    if (!logs || logs.length === 0) return;

    // 完了状態のチェック
    const hasFinished = logs.some(l => 
        typeof l === 'string' && (l.includes('テストケースの実行が完了') || l.includes('[FINAL_REPORT]'))
    );

    if (hasFinished) {
        setProgress(100);
        setIsCompleted(true);
        setCurrentPhase("生成完了");
        // UX向上のため、完了後は少し遅れて自動的に閉じる
        if (isOpen) setTimeout(() => setIsOpen(false), 1500);
        return;
    }

    // 最新のフェーズ情報を逆順で検索
    for (let i = logs.length - 1; i >= 0; i--) {
      const log = logs[i];
      if (typeof log === 'string') {
        // バックエンドの出力形式に合わせてフェーズを抽出
        const match = log.match(/\*\*フェーズ\s*([\d\.]+)([:：])\s*(.*?)\.*\*\*/);
        if (match) {
          setCurrentPhase(`フェーズ ${match[1]}: ${match[3]}`);
          if (PHASE_MAP[match[1]]) setProgress(PHASE_MAP[match[1]]);
          break; 
        }
      }
    }
  }, [logs]);

  // ステータスバーのレンダリング
  const renderStatusContent = (isOverlay) => (
    <div className="status-inner-content">
      <div className="status-info">
        <span className="status-icon-wrapper">
          {isCompleted ? <CheckIcon /> : <RocketIcon />}
        </span>
        <span className="status-text" title={currentPhase}>{currentPhase}</span>
      </div>
      <button 
        className={`toggle-log-btn ${isOverlay ? 'btn-on-blue' : 'btn-on-white'}`}
        onClick={(e) => { e.stopPropagation(); setIsOpen(!isOpen); }}
      >
        {isOpen ? "詳細を閉じる" : "詳細を表示"}
      </button>
    </div>
  );

  return (
    <div className="log-bubble-container">
      {/* プログレスバーヘッダー */}
      <div className="status-header-wrapper">
        <div className="status-layer base-layer">{renderStatusContent(false)}</div>
        <div className="status-layer overlay-layer" style={{ width: `${progress}%` }}>
            <div className="overlay-fixed-width-container">{renderStatusContent(true)}</div>
        </div>
      </div>

      {/* 折りたたみ可能なログエリア */}
      <div className={`log-collapsible-wrapper ${isOpen ? 'open' : ''}`}>
        <div className="log-title-small">実行詳細 & リアルタイム画面</div>
        <div ref={logRef} className="log-area-inline">
          {logs && logs.length > 0 ? (
            logs.map((log, index) => {
              // スクリーンショットのレンダリング処理
              if (typeof log === 'string' && log.startsWith('[SCREENSHOT]')) {
                const base64Img = log.replace('[SCREENSHOT]', '');
                return (
                  <div key={index} className="log-screenshot-container">
                    <img 
                        src={`data:image/png;base64,${base64Img}`} 
                        alt="Process Screenshot" 
                        onClick={() => onImageClick && onImageClick(base64Img)} 
                        title="クリックして拡大"
                        style={{cursor: 'zoom-in', maxWidth: '100%'}}
                    />
                  </div>
                );
              }
              
              // 通常のテキストログ（Markdownの太字マークを除去して表示）
              const cleanLog = typeof log === 'string' ? log.replace(/\*\*/g, '') : log;
              return <p key={index} className="log-text-line" style={{margin: '4px 0'}}>{cleanLog}</p>;
            })
          ) : <p style={{ color: '#888', padding: '10px' }}>タスク開始待機中...</p>}
        </div>
        {logs && logs.length > 0 && <div className="scroll-indicator">全 {logs.length} 件の記録</div>}
      </div>
    </div>
  );
};

// --- 根拠表示コンポーネント（状態判定付き） ---
const ExpandableReason = ({ reason }) => {
  const [isOpen, setIsOpen] = useState(true);

  // reason が空または undefined の場合、無効化されたボタンを表示
  if (!reason) {
    return (
        <div className="report-action-bar">
            <span className="reason-toggle-btn disabled">追加の判断根拠なし</span>
        </div>
    );
  }

  return (
    <div className="report-reason-container">
      <div className={`reason-anim-wrapper ${isOpen ? 'open' : ''}`}>
        <div className="reason-anim-inner">
          <div className="reason-content-box">
            <strong>判断根拠：</strong>{reason}
          </div>
        </div>
      </div>
      <div className="report-action-bar">
        <button 
          className="reason-toggle-btn" 
          onClick={(e) => { e.stopPropagation(); setIsOpen(!isOpen); }}
        >
          {isOpen ? '根拠を閉じる' : 'AI 判断根拠'} 
          <span style={{ 
            display: 'inline-block', 
            transform: isOpen ? 'rotate(180deg)' : 'rotate(0deg)', 
            transition: 'transform 0.3s',
            marginLeft: '4px'
          }}>▼</span>
        </button>
      </div>
    </div>
  );
};


// --- 構造化レポートコンポーネント ---
const StructuredReport = ({ text }) => {
  const processLines = (rawText) => {
    if (!rawText) return [];
    const rawLines = rawText.split('||NEWLINE||');
    const blocks = [];
    let currentListGroup = [];

    const flushListGroup = () => {
      if (currentListGroup.length > 0) {
        blocks.push({ type: 'grid-list', items: [...currentListGroup] });
        currentListGroup = [];
      }
    };

    for (let i = 0; i < rawLines.length; i++) {
      let line = rawLines[i].trim();
      if (!line) continue;

      // 独立した理由行の処理（||REASON|| が単独行として存在する場合）
      if (line.startsWith('||REASON||')) {
        const detachedReason = line.replace('||REASON||', '');
        // ケース1: 理由が現在のリストグループに属する場合（例: 企業情報リストの後の理由）
        if (currentListGroup.length > 0) {
            flushListGroup(); 
            // 生成された Grid ブロックに理由を付与
            if (blocks.length > 0) blocks[blocks.length - 1].reason = detachedReason;
        } 
        // ケース2: 理由が直前の通常ブロックに属する場合（例: リスク情報の後の理由）
        else if (blocks.length > 0) {
            blocks[blocks.length - 1].reason = detachedReason;
        }
        continue;
      }

      // 通常解析
      const parts = line.split('||REASON||');
      const mainText = parts[0];
      const inlineReason = parts.length > 1 ? parts[1] : null;

      if (mainText.match(/^\d+[、.]/)) {
        currentListGroup.push({ text: mainText, reason: inlineReason });
      } else {
        flushListGroup();
        
        // 日本語のキーワードに合わせてブロックタイプを判定
        if (mainText.includes('分析') || mainText.includes('戦略') || mainText.includes('ロジック')) {
             blocks.push({ type: 'analysis-header', text: mainText, reason: inlineReason });
        }
        // バックエンド出力の「チェック」に合わせて特殊KVとして処理
        else if (mainText.includes('：') && mainText.split('：')[1].trim().startsWith('チェック')) {
             blocks.push({ type: 'kv-special', text: mainText, reason: inlineReason });
        }
        else if (mainText.endsWith('：') || mainText.endsWith(':')) {
             blocks.push({ type: 'title', text: mainText, reason: inlineReason });
        }
        else if (mainText.includes('：')) {
             blocks.push({ type: 'kv', text: mainText, reason: inlineReason });
        }
        else {
             blocks.push({ type: 'text', text: mainText, reason: inlineReason });
        }
      }
    }
    flushListGroup();
    return blocks;
  };

  const blocks = processLines(text);

  return (
    <div className="report-card">
      <div className="report-header">スクリーニング条件の提案</div>
      <div className="report-body">
        {blocks.map((block, index) => {
          
          // 1. 特殊 KV (タイトル上、緑枠下) - 例: 登録ステータス
          if (block.type === 'kv-special') {
            const [label, value] = block.text.split(/：(.+)/);
            return (
              <div key={index}>
                 <div className="report-section-title">{label}</div>
                 <div className="report-green-block">
                    <div className="report-block-text">{value}</div>

                 </div>
                    {/* 理由コンポーネントを強制レンダリング */}
                    <ExpandableReason reason={block.reason} />
              </div>
            );
          }

          // 2. タイトル (例: 経営情報)
          if (block.type === 'title') {
            return (
              <div key={index}>
                <div className="report-section-title" style={{marginTop: '12px'}}>{block.text.replace('：','')}</div>
                {block.reason && (
                    <div className="report-green-block" style={{borderLeftColor:'#007bff', background:'#f4f8fb'}}>
                        <ExpandableReason reason={block.reason} />
                    </div>
                )}
              </div>
            );
          }

          // 3. 分析ブロック (例: 詳細スクリーニング戦略)
          if (block.type === 'analysis-header') {
             return (
                <div key={index} className="report-analysis-card">
                    <span className="report-analysis-title">{block.text.replace('：','')}</span>
                    <div>{block.reason || "詳細なし"}</div>
                </div>
             );
          }

          // 4. 通常 KV (例: キーワード、省) - 理由を表示するように制御
          if (block.type === 'kv') {
             const [label, value] = block.text.split(/：(.+)/);
             return (
                <div key={index} style={{marginBottom: 12}}>
                    <div className="report-key-value">
                        <span className="report-label">{label}：</span>
                        <span className="report-value">{value}</span>
                    </div>
                    {/* 理由ボタンをレンダリング */}
                    <ExpandableReason reason={block.reason} />
                </div>
             );
          }

          // 5. Grid リスト (例: 詳細オプション) - リスト後に理由を表示
          if (block.type === 'grid-list') {
            return (
              <div key={index}>
                  <div className="report-grid-container">
                    {block.items.map((item, i) => (
                      <div key={i} className="report-grid-item">{item.text}</div>
                    ))}
                  </div>
                  {/* Grid の下に理由をレンダリング */}
                  <ExpandableReason reason={block.reason} />
              </div>
            );
          }

          // 6. フォールバック
          return (
            <div key={index} className="report-text">{block.text}</div>
          );

        })}
      </div>
    </div>
  );
};



/**
 * 引用表示コンポーネント
 * 文中には小さな [1] マークのみを表示し、ホバー時に詳細を表示します。
 */
const MessageWithCitations = ({ text }) => {
  if (!text) return null;

  // 正規表現: 
  // 1. [ ... p.数字 ... ] : ページ番号付き引用
  // 2. [ ... .pdf/jpg ... ] : 拡張子付きファイル名引用（ページ番号なし）
  // ※ jpg を追加済み
  const regex = /(\[[^\]]+? p\.[\d,\s]+\]|\[[^\]]+?\.(?:pdf|xlsx|xls|docx|doc|pptx|ppt|txt|csv|jpg)\])/gi;

  const parts = text.split(regex);
  
  // 引用番号のカウンタを初期化
  let citationCount = 0;

  return (
    <span>
      {parts.map((part, index) => {
        if (part.match(regex)) {
          // マッチした場合のみカウントアップ
          citationCount++;
          
          // ブラケットを除去して中身だけにする (例: "report.pdf p.12")
          const content = part.replace(/[\[\]]/g, '');
          
          return (
            <span key={index} className="citation-wrapper">
              {/* 番号を表示 */}
              <span className="citation-trigger footnote">
                [{citationCount}]
              </span>
              
              {/* ツールチップ */}
              <span className="citation-tooltip">
                {content}
              </span>
            </span>
          );
        }
        return part;
      })}
    </span>
  );
};

// --- App メインコンポーネント ---
function App() {
  
  const [password, setPassword] = useState(localStorage.getItem('app_password') || '');
  const [isAuthenticated, setIsAuthenticated] = useState(!!localStorage.getItem('app_password'));
  const [loginError, setLoginError] = useState('');

  // ==========================================
  // セッション（履歴）管理の状態
  // ==========================================
  const[sessions, setSessions] = useState(() => {
    const saved = localStorage.getItem('chat_sessions');
    return saved ? JSON.parse(saved) : [];
  });
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [prevSessionId, setPrevSessionId] = useState(null);
  const [messages, setMessages] = useState([]); 
  
  const [userInput, setUserInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const[aiState, setAiState] = useState('idle'); 
  const [thinkingText, setThinkingText] = useState('');
  const [currentLogMessages, setCurrentLogMessages] = useState([]); 
  const [previewImage, setPreviewImage] = useState(null);
  
  // モバイル用サイドバー開閉状態
  const [isSidebarOpen, setIsSidebarOpen] = useState(false);
  // 通知バーの表示状態 
  const [showNotice, setShowNotice] = useState(true);

  const logRef = useRef(null); 
  const messagesEndRef = useRef(null); 
  const messagesStartRef = useRef(null); 

  // バックエンドから受け取る動的なサジェストプロンプト
  const [dynamicPrompts, setDynamicPrompts] = useState([]);

  // 初期化：セッションが存在しない場合は新規作成
  useEffect(() => {
    if (sessions.length === 0) {
      createNewSession();
    } else if (!currentSessionId) {
      // 最後に更新されたセッションを読み込む
      const lastSession = [...sessions].sort((a, b) => b.updatedAt - a.updatedAt)[0];
      switchSession(lastSession.id);
    }
  },[]);

  // messages が更新されたら、現在のセッションに保存（localStorageへ同期）
  useEffect(() => {
    if (currentSessionId && messages.length > 0) {
      setSessions(prev => {
        const updated = prev.map(s => 
          s.id === currentSessionId ? { ...s, messages, updatedAt: Date.now() } : s
        );
        localStorage.setItem('chat_sessions', JSON.stringify(updated));
        return updated;
      });
    }
  },[messages, currentSessionId]);

// スクロール制御を高度化（最初の一言でトップにジャンプし、以降は自動追従）
useEffect(() => {
  if (currentSessionId !== prevSessionId) {
    // セッションが切り替わった場合（過去の履歴を開いた、または新規作成）
    setPrevSessionId(currentSessionId);
    
    if (messages.length > 0) {
      // 履歴がある場合は、最初のメッセージが上部（padding付）にくるように即座にスクロール
      setTimeout(() => {
        messagesStartRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' });
      }, 10);
    } else {
      // 新規セッションの場合は一番上（Hero UI）へ
      const area = document.querySelector('.messages-area');
      if (area) area.scrollTop = 0;
    }
  } else {
    // 同じセッション内でのメッセージ追加やAIの思考状態の変化
    if (messages.length === 1 && aiState === 'thinking') {
      // 最初のメッセージ送信時：Hero UIを画面外へ押し出すようにスクロール
      setTimeout(() => {
        messagesStartRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      }, 50);
    } else if (messages.length > 0) {
      // 通常時：メッセージが追加されたら下へ追従
      if (messagesEndRef.current) {
        messagesEndRef.current.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
      }
    }
  }
}, [messages, aiState, currentSessionId, prevSessionId]);



  // 新規セッション作成
  const createNewSession = () => {
    const hasEmpty = sessions.some(s => !s.messages || s.messages.length === 0);
    if (hasEmpty) return; // 空セッションが存在する場合は無効化

    const newId = `sess_${Date.now()}`;
    const newSession = {
      id: newId,
      title: "新しいチャット",
      messages:[],
      updatedAt: Date.now()
    };
    setSessions(prev => {
      const updated = [newSession, ...prev];
      localStorage.setItem('chat_sessions', JSON.stringify(updated));
      return updated;
    });
    setCurrentSessionId(newId);
    setMessages([]);
    if (window.innerWidth <= 768) setIsSidebarOpen(false); // スマホ時は閉じる
  };

  // セッション切り替え
  const switchSession = (id) => {
    const target = sessions.find(s => s.id === id);
    if (target) {
      setCurrentSessionId(id);
      setMessages(target.messages ||[]);
      if (window.innerWidth <= 768) setIsSidebarOpen(false);
    }
  };

  // セッション削除
  const deleteSession = (id) => {
    const updated = sessions.filter(s => s.id !== id);
    setSessions(updated);
    localStorage.setItem('chat_sessions', JSON.stringify(updated));
    if (currentSessionId === id) {
      if (updated.length > 0) {
        switchSession(updated[0].id);
      } else {
        createNewSession();
      }
    }
  };



  const handleLogin = async () => {
    if (!password.trim()) {
        setLoginError('パスワードを入力してください。');
        return;
    }
    try {
        const response = await fetch(`${API_BASE_URL}/verify-password`, {
            method: 'GET',
            headers: { 'x-api-key': password }
        });
        if (response.ok) {
            localStorage.setItem('app_password', password);
            setIsAuthenticated(true);
            setLoginError('');
        } else {
            setLoginError('パスワードが間違っています / Invalid Password');
        }
    } catch (error) {
        setLoginError('サーバーに接続できません / Server Error');
    }
  };

  // LLMを使用したバックグラウンドタイトル生成
  const generateTitleWithLLM = async (text, sessionId) => {
    try {
      // ストリーミングAPIを非同期で叩いてタイトルを抽出
      const response = await fetch(API_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-api-key': password },
        body: JSON.stringify({ 
            message: `以下のユーザー入力の意図を汲み取り、このチャットの短いタイトル（10文字以内の文字列のみ）を作成してください。説明や記号は不要です。\n入力内容：${text}`,
            session_id: `title_${Date.now()}` 
        }),
      });
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let generatedTitle = '';
      
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let eventEndIndex;
        while ((eventEndIndex = buffer.indexOf('\n\n')) !== -1) {
          const event = buffer.substring(0, eventEndIndex);
          buffer = buffer.substring(eventEndIndex + 2);
          if (event.includes('[TEXT_RESPONSE]')) {
            const parts = event.split('[TEXT_RESPONSE]');
            if (parts.length > 1) {
                generatedTitle += parts[1];
            }
         }
       }
     }
      
      const finalTitle = generatedTitle.replace(/\\n/g, '').trim() || text.slice(0, 10) + '...';
      
      setSessions(prev => {
        const updated = prev.map(s => s.id === sessionId ? { ...s, title: finalTitle } : s);
        localStorage.setItem('chat_sessions', JSON.stringify(updated));
        return updated;
      });
    } catch (e) {
      console.error("Title generation failed", e);
    }
  };

  const handleSendMessage = async (overrideText = null) => {
    const isStringOverride = typeof overrideText === 'string';
    const promptText = isStringOverride ? overrideText : userInput;
    if (!promptText.trim()) return;

    const userMessage = { sender: USER_NAME, text: promptText, type: 'text', id: Date.now() };
    
    // タイトル生成（新規セッションの最初の発言時）
    if (messages.length === 0) {
      const firstLine = promptText.split('\n').find(line => line.trim().length > 0) || promptText;
      const newTitle = firstLine.length > 15 ? firstLine.slice(0, 15) + '...' : firstLine;
      setSessions(prev => prev.map(s => s.id === currentSessionId ? { ...s, title: newTitle } : s));
    }

    setMessages((prev) => [...prev, userMessage]);
    if (!isStringOverride) setUserInput(''); 
    
    // 送信時に動的プロンプトをリセット
    setDynamicPrompts([]);

    setIsLoading(true);
    setAiState('thinking'); 
    setThinkingText('');

    let tempAiMsgId = Date.now() + 1;
    let isToolRunning = false;
    let incomingTextResponse = "";
    
    try {
      const response = await fetch(API_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-api-key': password },
        body: JSON.stringify({ message: promptText, session_id: currentSessionId }),
      });

      if (response.status === 401) {
        throw new Error('401'); // 下部の catch ブロックへ回す
      }
      if (!response.ok) throw new Error(`HTTP error! status: ${response.status}`);

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let fullLogHistory = [];

      setCurrentLogMessages([]); // 現在のログストリームをクリア

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        let eventEndIndex;
        
        while ((eventEndIndex = buffer.indexOf('\n\n')) !== -1) {
          const event = buffer.substring(0, eventEndIndex);
          buffer = buffer.substring(eventEndIndex + 2);
          if (event.startsWith('data:')) {
            const logLine = event.substring(5).trim();
            
            if (logLine === "---END_OF_STREAM---") {
                break;
            }

            // サジェストプロンプトの受信処理
            if (logLine.startsWith('[SUGGEST_PROMPTS]')) {
              const jsonStr = logLine.replace('[SUGGEST_PROMPTS]', '');
              try {
                  setDynamicPrompts(JSON.parse(jsonStr));
              } catch (e) {
                  console.error("Suggest Prompts JSON Error", e);
              }
              continue;
            }

            // --- ステータスメッセージの処理 ---
            if (logLine.startsWith('[STATUS_MSG]')) {
                const noteText = logLine.replace('[STATUS_MSG]', '');
                // システム通知メッセージを追加
                setMessages(prev => [...prev, { type: 'system_note', text: noteText }]);
                continue;
            }

            // --- RAGヒット通知の処理 ---
            if (logLine.startsWith('[RAG_HIT]')) {
                const hitText = logLine.replace('[RAG_HIT]', '');
                // RAGヒット時はsystem_noteとして表示
                setMessages(prev => [...prev, { type: 'system_note', text: hitText, isSuccess: true }]);
                continue;
            }


            // --- 提案カードデータの解析ロジック---
            if (logLine.startsWith('[PROPOSAL_DATA]')) {
              const jsonStr = logLine.replace('[PROPOSAL_DATA]', '');
              try {
                  const data = JSON.parse(jsonStr);
                  setMessages(prev => [...prev, { 
                      sender: AI_ASSISTANT_NAME, 
                      type: 'proposal_card',
                      data: data 
                  }]);
              } catch (e) {
                  console.error("Proposal JSON Error", e);
              }
              continue;
            }

            // --- CRMカードの処理 ---
            if (logLine.startsWith('[DB_CARD_DATA]')) {
              const jsonStr = logLine.replace('[DB_CARD_DATA]', '');
              try {
                  const cardData = JSON.parse(jsonStr);
 
                  setMessages(prev => [...prev, { 
                      sender: AI_ASSISTANT_NAME, 
                      type: 'crm_card_list', 
                      data: cardData 
                  }]);
              } catch (e) {
                  console.error("DB Card JSON Error", e);
              }
              continue;
            }

            // --- 特殊制御マーカーの処理 ---

            // 1. [Thinking] マーカー
            if (logLine.startsWith('[Thinking]')) {
                setAiState('thinking');
                
                const t_text = logLine.replace('[Thinking]', '').trim();
                if (t_text) {
                    setThinkingText(t_text);
                }
                
                continue; 
            }

            // 2. [TEXT_RESPONSE] マーカー (通常会話)
            if (logLine.startsWith('[TEXT_RESPONSE]')) {
              setAiState('responding');
              let newText = logLine.replace('[TEXT_RESPONSE]', '').replace(/\\n/g, '\n');
              incomingTextResponse += newText;
              
              setMessages(prev => {
                  const lastMsg = prev[prev.length - 1];
                  // 直近のメッセージが生成中のAIテキストの場合、内容を更新
                  if (lastMsg && lastMsg.type === 'text' && lastMsg.sender === AI_ASSISTANT_NAME && lastMsg._tempId === tempAiMsgId) {
                      return prev.map(m => m._tempId === tempAiMsgId ? { ...m, text: incomingTextResponse } : m);
                  }
                  // それ以外（system_note挿入後など）は新規メッセージとして追加
                  return [...prev, { sender: AI_ASSISTANT_NAME, text: incomingTextResponse, type: 'text', _tempId: tempAiMsgId }];
              });
              continue;
            }

            // 3. ツール実行ログ (ログ受信開始をトリガーにツール起動とみなす)
            if (!isToolRunning) {
                isToolRunning = true;
                setAiState('executing');
                
                // ツール実行用のプロセス用メッセージを作成
                setMessages(prev => [...prev, { 
                    sender: AI_ASSISTANT_NAME, 
                    text: "ご要望に合わせて、スクリーニング条件の生成を開始します", 
                    type: 'process_running',
                    logId: Date.now() 
                }]);
            }

            // 4. 通常ログとレポートの処理
            if (logLine.startsWith('[FINAL_REPORT]')) {
              let reportContent = logLine.replace('[FINAL_REPORT]', '');
              setMessages(prev => [...prev, { sender: AI_ASSISTANT_NAME, text: reportContent, type: 'report' }]);
            } else {
                // 通常ログ
                fullLogHistory.push(logLine);
                setCurrentLogMessages(prev => [...prev, logLine]);
            }
          }
        }
      }

      // ストリーム終了後、ログを対応するprocessメッセージに保存
      if (isToolRunning) {
          setMessages(prev => prev.map(msg => {
             if (msg.type === 'process_running' && !msg.savedLogs) {
                 return { ...msg, savedLogs: fullLogHistory };
             }
             return msg;
          }));
          setMessages(prev => [...prev, { sender: AI_ASSISTANT_NAME, text: "申し訳ございません。クラウドサーバーが中国以外のIPのため、中国の法規制により、現在qcc.comの中国企業検索ページをご利用いただけません。", type: 'text' }]);
      }

    } catch (error) {
      // 【最適化】エラー発生時（ネットワーク切れ、パスワード変更等）は、今送ったメッセージを削除（ロールバック）
      setMessages(prev => prev.filter(m => m.id !== userMessage.id));
      
      if (error.message.includes('Failed to fetch') || error.message.includes('401')) {
        localStorage.removeItem('app_password'); 
        setIsAuthenticated(false);               
        setLoginError('認証エラー：パスワードが無効です。再度入力してください。');
      } else {
        setMessages(prev =>[...prev, { sender: AI_ASSISTANT_NAME, text: `エラーが発生しました: ${error.message}`, type: 'text' }]);
      }
    } finally {
      setIsLoading(false);
      setAiState('idle');
    }
  };

  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !isLoading) handleSendMessage();
  };

  const closePreview = () => setPreviewImage(null);

// 値のフォーマット（金額などを整形）
const formatValue = (key, value) => {
  if (value === null || value === undefined) return '-';
  // 金額っぽいキーの場合、カンマ区切りにする
  if (/amount|sales|price|売上|金額/.test(key.toLowerCase()) && !isNaN(value)) {
      return `¥${Number(value).toLocaleString()}`;
  }
  return value;
};

// ステータスに応じたクラス名を返す（CSSで色を制御するため）
const getStatusClass = (status) => {
  if (!status) return 'gray';
  if (/契約|成約|已签约/.test(status)) return 'green';
  if (/商談|交渉|進行/.test(status)) return 'blue';
  if (/失注|失敗/.test(status)) return 'red';
  return 'gray';
};


// --- データベース項目名 日本語マッピング定義 ---
const FIELD_MAPPING = {
  // 企業情報
  "id": "ID",
  "name": "企業名",
  "company_name": "企業名",
  "industry": "業種",
  "region": "地域",
  "established_year": "設立年",
  
  // 担当者情報
  "contact_person": "担当者名",
  "pic": "担当者名", // 旧互換
  "position": "役職",
  "email": "メールアドレス",
  "phone": "電話番号",
  
  // 商談・売上情報
  "status": "商談状況",
  "deal_status": "商談状況",
  "sales_amount": "商談金額",
  "amount": "金額",
  "last_contact_date": "最終接触日",
  "last_contact": "最終接触日",
  "product_category": "製品カテゴリ",
  
  // その他システム系
  "created_at": "登録日時",
  "updated_at": "更新日時"
};

// ラベル取得ヘルパー関数
const getLabel = (key) => {
  return FIELD_MAPPING[key] || key; // マッピングがなければそのまま英語キーを表示
};

// 全セッションの中から、空のセッションが1つでも存在するかチェック
const hasEmptySession = sessions.some(s => !s.messages || s.messages.length === 0);


if (!isAuthenticated) {
  return (

    <>

    {showNotice && (
      <div className="top-notification-bar" style={{ position: 'fixed', top: 0, width: '100%', zIndex: 3000 }}>
        <span className="notification-icon">⚠️</span>
        <span className="notification-text">{SYSTEM_NOTICE}</span>
        <span className="notification-dismiss" onClick={() => setShowNotice(false)}>閉じる</span>
      </div>
    )}



      <div className="login-overlay">
          <div className="login-card">
              <h2>アクセス認証</h2>
              <input 
                  type="password" 
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="パスワード / Password"
                  className="login-input"
                  onKeyPress={(e) => { if (e.key === 'Enter') handleLogin(); }}
              />
              <button className="login-btn" onClick={handleLogin}>ログイン / Login</button>
              {loginError && <div className="login-error">{loginError}</div>}
          </div>
      </div>
      </>
  );
}


const hasQuickPrompts = messages.length === 0 || dynamicPrompts.length > 0;

return (
  <div className="app-layout-wrapper">
    
    {/* 1. 通知バー（表示状態が true の時だけレンダリング） */}
    {showNotice && (
      <div className="top-notification-bar">
        <span className="notification-icon">⚠️</span>
        <span className="notification-text">{SYSTEM_NOTICE}</span>
        {/* 閉じるボタン */}
        <span className="notification-dismiss" onClick={() => setShowNotice(false)}>閉じる</span>
      </div>
    )}

    <div className="main-content-area">
      {/* モバイル用サイドバーのオーバーレイ背景 */}
      {isSidebarOpen && <div className="sidebar-overlay" onClick={() => setIsSidebarOpen(false)}></div>}

      {/* 2. 左側の履歴サイドバー */}
      <div className={`history-sidebar ${isSidebarOpen ? 'open' : ''}`}>
         <div className="sidebar-header">
            {/* 現在のセッションが空の場合はボタンを無効化 */}
            <button 
              className="new-chat-btn" 
              onClick={createNewSession}
              disabled={hasEmptySession}
              style={{ opacity: hasEmptySession ? 0.5 : 1, cursor: hasEmptySession ? 'not-allowed' : 'pointer' }}
            >
               <span className="plus-icon">+</span> 新しいチャット
            </button>
         </div>
         
         <div className="session-list-container">
            {sessions.length === 0 ? (
              <div className="empty-session-text">チャット履歴がありません</div>
            ) : (
              sessions.map(s => (
                <div 
                  key={s.id} 
                  className={`session-item ${currentSessionId === s.id ? 'active' : ''}`}
                  onClick={() => switchSession(s.id)}
                >
                   <div className="session-title">{s.title || "新しいチャット"}</div>
                   <button 
                      className="delete-session-btn" 
                      onClick={(e) => { e.stopPropagation(); deleteSession(s.id); }}
                      title="削除"
                   >×</button>
                </div>
              ))
            )}
         </div>
      </div>

      {/* 3. 右側のチャットメインエリア */}
      <div className="chat-container">
        
        {/* モバイル時に表示されるヘッダーバー */}
        <div className="mobile-chat-header">
           <button className="menu-btn" onClick={() => setIsSidebarOpen(true)}>
             <MenuIcon />
           </button>
           <span className="mobile-header-title">
              {sessions.find(s => s.id === currentSessionId)?.title || "新しいチャット"}
           </span>
        </div>
            
        <div className={`messages-area ${hasQuickPrompts ? 'has-prompts' : 'no-prompts'}`}>
          <div className="welcome-screen">
            <h1 className="welcome-title">どのようなサポートが必要ですか？</h1>
            <div className="feature-cards-container">
              <div className="feature-card">
                <img src={ICON_PROFILE} alt="Profile" className="feature-icon-img" />
                <h3 className="feature-title">顧客プロファイリング</h3>
                <p className="feature-desc">B2B営業の専門家として、業界ナレッジベースに基づき最適なターゲット顧客像をご提案します。</p>
              </div>
              <div className="feature-card">
                <img src={ICON_SEARCH} alt="Search" className="feature-icon-img" />
                <h3 className="feature-title">自動スクリーニング</h3>
                <p className="feature-desc">ターゲット条件に基づき、企業検索サイトで絞り込み条件を自動設定し、企業選定を支援します。</p>
              </div>
              <div className="feature-card">
                <img src={ICON_ANALYZE} alt="Analyze" className="feature-icon-img" />
                <h3 className="feature-title">営業データ活用</h3>
                <p className="feature-desc">フォロー中の顧客情報をもとに、条件に応じた絞り込みや関連情報の検索をスムーズに行えます。</p>
              </div>
            </div>

            {/* RAGドキュメント一覧 */}
            <div className="rag-docs-container">
              <div className="rag-docs-title">ナレッジベース一覧</div>
              <div className="rag-docs-list">
                <span className="rag-doc-tag">中国華南地域の水素燃料電池産業</span>
                <span className="rag-doc-tag">中国経済と日本企業2025年白書</span>
                <span className="rag-doc-tag">太陽光発電企業サプライチェーン</span>
                <span className="rag-doc-tag">工作機械産業地域別概況.xlsx</span>
                <span className="rag-doc-tag">広州美容産業レポート.pdf</span>
                <span className="rag-doc-tag">自動車ガラス産業チェーン.txt</span>
                <span className="rag-doc-tag">人型ロボット産業チェーンガイド.txt</span>
              </div>
            </div>



          </div>
         
          {messages.length > 0 && (
            <div className="messages-list-wrapper" ref={messagesStartRef}>
              {messages.map((msg, index) => {
            const isUser = msg.sender === USER_NAME;
            const isReportMsg = msg.type === 'report';
            const isProcessMsg = msg.type === 'process_running';
     
            let logs = [];
            if (isProcessMsg) {
                logs = msg.savedLogs ? msg.savedLogs : currentLogMessages;
            }

            return (
              <div 
                key={index} 
                className={`message-row ${isUser ? 'user' : 'ai'} ${msg.type === 'system_note' ? 'system-row' : ''}`}
              >
                  {/* 分岐1: システムステータス通知 (RAG状態など) */}
                  {msg.type === 'system_note' ? (
                      <div className="system-note-container">
                          <span className={`system-note-text ${msg.isSuccess ? 'success-note' : ''}`}>
                              {msg.text}
                          </span>
                      </div>
                  ) : 
                  /* 分岐2: CRM カードリストのレンダリング */
                  msg.type === 'crm_card_list' ? (
                    <div className="message-row ai" style={{width: '100%', display: 'flex', gap: '12px'}}>
                        <div className="avatar" style={{ backgroundImage: `url(${AI_AVATAR})` }}></div>
                        <div className="message-content" style={{width: '100%'}}>
                            <div className="sender-name">{msg.sender}</div>
                            
                            {/* --- データ表示ロジック開始 --- */}
                            {(() => {
                                const dataList = msg.data || [];
                                if (dataList.length === 0) return <div>データがありません</div>;

                                // 1. フィールド情報の解析
                                const allKeys = Object.keys(dataList[0]);
                                
                                // プライマリキー（タイトル/左端にする項目）の推定ロジック
                                // 'name', 'company', 'id' 等が含まれるキーを優先
                                const primaryKey = allKeys.find(k => /name|company|title|社名|企業名/.test(k.toLowerCase())) 
                                                || allKeys.find(k => /id|no|code/.test(k.toLowerCase())) 
                                                || allKeys[0];

                                // ステータスキーの推定（色分け用）
                                const statusKey = allKeys.find(k => /status|state|状況|状態/.test(k.toLowerCase()));

                                // 表示から除外するキー（プライマリキーやステータスは別途扱うため）
                                const bodyKeys = allKeys.filter(k => k !== primaryKey && k !== statusKey);

                                // --- A. テーブル表示モード (データが3件以上の場合) ---
                                if (dataList.length > 2) {
                                  return (
                                      <div className="crm-table-wrapper">
                                          <table className="crm-table">
                                              <thead>
                                                  <tr>
                                                      {/* 修正: key を getLabel(key) に変更 */}
                                                      <th className="fixed-col">{getLabel(primaryKey)}</th>
                                                      {statusKey && <th>{getLabel(statusKey)}</th>}
                                                      {bodyKeys.map(key => <th key={key}>{getLabel(key)}</th>)}
                                                  </tr>
                                              </thead>
                                              {/* tbody は変更なし（データの中身はそのまま表示するため） */}
                                              <tbody>
                                                  {dataList.map((item, i) => (
                                                      <tr key={i}>
                                                          <td className="primary-cell">{item[primaryKey]}</td>
                                                          {statusKey && (
                                                              <td>
                                                                  <span className={`status-tag table-tag ${getStatusClass(item[statusKey])}`}>
                                                                      {item[statusKey]}
                                                                  </span>
                                                              </td>
                                                          )}
                                                          {bodyKeys.map(key => (
                                                              <td key={key}>{formatValue(key, item[key])}</td>
                                                          ))}
                                                      </tr>
                                                  ))}
                                              </tbody>
                                          </table>
                                      </div>
                                  );
                              }

                                // --- B. カード表示モード (データが4件以下の場合) ---
                                return (
                                    <div className="crm-list-container">
                                        {dataList.map((item, i) => {
                                            const statusVal = statusKey ? item[statusKey] : null;
                                            
                                            // ステータスに応じた色設定（ヘルパー関数化してもよいが、ここではインラインで定義）
                                            let statusColor = '#cbd5e0'; 
                                            if (statusVal) {
                                                if (/契約|成約|已签约/.test(statusVal)) statusColor = '#38a169'; // 緑
                                                else if (/商談|交渉|進行/.test(statusVal)) statusColor = '#3182ce'; // 青
                                                else if (/失注|失敗/.test(statusVal)) statusColor = '#e53e3e'; // 赤
                                            }

                                            return (
                                                <div key={i} className="crm-list-item">
                                                    {/* 左側のアクセントバー */}
                                                    <div className="crm-status-bar" style={{backgroundColor: statusColor}}></div>

                                                    {/* ヘッダー：企業名とバッジ */}
                                                    <div className="crm-item-header">
                                                        <span className="crm-company-name">{item[primaryKey]}</span>
                                                        {statusVal && (
                                                            <span className={`crm-status-badge ${getStatusClass(statusVal)}`}>
                                                                {statusVal}
                                                            </span>
                                                        )}
                                                    </div>

                                                    {/* ボディ：動的グリッド */}
                                                    <div className="crm-item-body">
                                                        {bodyKeys.map((key) => (
                                                            <div key={key} className="crm-info-row">
                                                                <span className="crm-label">{getLabel(key)}</span>
                                                                <span className="crm-value">
                                                                    {formatValue(key, item[key])}
                                                                </span>
                                                            </div>
                                                        ))}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                );
                            })()}
                            {/* --- データ表示ロジック終了 --- */}

                            <div style={{fontSize: '0.8em', color: '#999', marginTop: '8px', textAlign: 'right'}}>
                                検索結果: {msg.data.length} 件
                            </div>
                        </div>
                    </div>
                ) :
                /* 分岐3: 提案カード */
                msg.type === 'proposal_card' ? (
                  <div className="message-row ai" style={{width: '100%'}}>
                      <div className="avatar" style={{ backgroundImage: `url(${AI_AVATAR})` }}></div>
                      <div className="message-content">
                          <div className="proposal-card-container">
                              {/* ヘッダー：アイコンを変更し、よりレポートらしく */}
                              <div className="proposal-header">
                                  <span>スクリーニング条件の提案</span>
                              </div>

                              {/* ボディ */}
                              <div className="proposal-body">
                                  
                                  {/* ターゲット定義ブロック：図2の「判断根拠」風スタイル */}
                                  <div className="proposal-section">
                                      <div className="proposal-label">
                                          ターゲット定義
                                      </div>
                                      <div className="proposal-guidance-box">
                                          {msg.data.guidance}
                                      </div>
                                  </div>

                                  {/* 地域タグ */}
                                  <div className="proposal-section">
                                      <div className="proposal-label">地域指定</div>
                                      <div className="proposal-tags-wrapper">
                                          {msg.data.regions && msg.data.regions.length > 0 ? (
                                              msg.data.regions.map((region, idx) => (
                                                  <span key={idx} className="proposal-tag region">{region}</span>
                                              ))
                                          ) : (
                                              <span className="proposal-tag region">全国</span>
                                          )}
                                      </div>
                                  </div>

                                  {/* キーワードタグ */}
                                  <div className="proposal-section">
                                      <div className="proposal-label">抽出キーワード</div>
                                      <div className="proposal-tags-wrapper">
                                          {/* キーワード文字列を配列に分割してタグ表示 */}
                                          {(msg.data.keywords || '自動生成').split(/[,、]/).map((kw, idx) => {
                                              const cleanKw = kw.trim();
                                              if(!cleanKw) return null;
                                              return <span key={idx} className="proposal-tag keyword">{cleanKw}</span>;
                                          })}
                                      </div>
                                  </div>

                                  {/* 注釈 */}
                                  <div className="proposal-note">
                                      ※ 条件を変更したい場合は、チャットで直接指示してください（例：「地域を上海に変更して」）。
                                  </div>

                                  {/* 実行ボタン */}
                                  <button 
                                      className="proposal-btn"
                                      onClick={() => handleSendMessage("条件を確認しました。スクリーニングを開始してください。")}
                                      disabled={isLoading}
                                  >
                                      条件を確定して検索開始
                                  </button>
                              </div>
                          </div>
                      </div>
                  </div>
              ) :
                
                
                
                (
                  
                      /* 分岐4: 通常メッセージ (アバター + 吹き出し) */
                      <>
                          <div className="avatar" style={{ backgroundImage: `url(${isUser ? USER_AVATAR : AI_AVATAR})` }}></div>
                          <div className="message-content">
                              <span className="sender-name">{msg.sender}</span>
                              
                              {isProcessMsg ? (
                                <>
                                  <div className="message-bubble ai">
                                    <span>{msg.text}</span>
                                  </div>
                                  <div style={{ marginTop: '12px', width: '100%' }}> 
                                     <LogContent 
                                          logs={logs} 
                                          // 実行中のタスクの場合のみ自動スクロールを有効化
                                          logRef={(!msg.savedLogs && isLoading) ? logRef : null} 
                                          onImageClick={setPreviewImage}
                                      />
                                  </div>
                                </>
                              ) : (
                                <div className={`message-bubble ${isUser ? 'user' : 'ai'} ${isReportMsg ? 'report-mode' : ''}`}>
                                     {isReportMsg ? (
                                        <StructuredReport text={msg.text} />
                                     ) : (
                                        <MessageWithCitations text={msg.text} />
                                     )}
                                </div>
                              )}
                          </div>
                      </>
                  )}
              </div>
          );
        })}
      
      {/* Thinking 状態表示 */}
      {aiState === 'thinking' && (
          <div className="message-row ai">
              <div className="avatar" style={{ backgroundImage: `url(${AI_AVATAR})` }}></div>
              <div className="message-content">
                   <div className="message-bubble ai">
                       <span>考え中...</span>
                   </div>
              
                   {thinkingText && (
    <div style={{ 
        marginTop: '16px', 
        marginLeft: '4px', 
        display: 'flex', 
        alignItems: 'center', 
        gap: '6px'
        // maxWidth: '90%' 
    }}>
        <div 
            className="thinking-icon" 
            style={{
                width: '18px', 
                height: '18px', 
                WebkitMaskImage: `url(${THINKING_ICON})`,
                WebkitMaskSize: 'contain',
                WebkitMaskRepeat: 'no-repeat',
                WebkitMaskPosition: 'center',
                maskImage: `url(${THINKING_ICON})`,
                maskSize: 'contain',
                maskRepeat: 'no-repeat',
                maskPosition: 'center',
                flexShrink: 0
            }}
        ></div>
        <div style={{ fontSize: '0.85em', color: '#888', lineHeight: '1.4' }}>
            <span className="thinking-text" style={{ fontStyle: 'normal' }}>{thinkingText}</span>
        </div>
    </div>
)}
              </div>
          </div>
      )}
      
      <div ref={messagesEndRef} />
      
      
        </div> 
      )}

      </div> 

      <div className="input-section-wrapper">
        {/* メッセージが空の時、または動的プロンプトがある時にクイックプロンプトを表示 */}
        {(messages.length === 0 || dynamicPrompts.length > 0) && (
          <div className="quick-prompts-container">
            {(messages.length === 0 ? QUICK_PROMPTS : dynamicPrompts).map((prompt, idx) => (
              <button 
                key={idx} 
                className="quick-prompt-btn"
                onClick={() => handleSendMessage(prompt.text)}
                disabled={isLoading}
              >
                {prompt.label}
              </button>
            ))}
          </div>
        )}
      
        {/* 下部入力エリア */}
        <div className="input-area">
          <input 
              type="text" 
              value={userInput} 
              onChange={(e) => setUserInput(e.target.value)} 
              onKeyPress={handleKeyPress} 
              placeholder={isLoading ? "回答を生成中..." : "探したい企業や条件を入力"} 
              disabled={isLoading}
          />
          <button onClick={() => handleSendMessage()} disabled={isLoading}>
              {isLoading ? '送信中' : '送信'}
          </button>
        </div>
      </div> 

    </div> 

  </div> 



{previewImage && (
        <div className="image-modal-overlay" onClick={closePreview}>
          <div className="image-modal-content" onClick={(e) => e.stopPropagation()}>
            <span className="close-button" onClick={closePreview}>&times;</span>
            <img src={`data:image/png;base64,${previewImage}`} alt="Enlarged Screenshot" />
          </div>
        </div>
      )}



</div> 
  );
}

export default App;