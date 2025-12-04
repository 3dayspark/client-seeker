

import React, { useState, useEffect, useRef } from 'react';
import './App.css';

const AI_ASSISTANT_NAME = "産業リサーチAI";
const USER_NAME = "自分";
const AI_AVATAR = `https://ui-avatars.com/api/?name=AI&background=0D8ABC&color=fff&size=128`;
const USER_AVATAR = `https://ui-avatars.com/api/?name=User&background=333&color=fff&size=128`;

// APIエンドポイントの設定（開発環境用）
// 本番環境へデプロイする際は、適切なドメインに変更してください
const API_ENDPOINT = "http://192.168.1.41:8000/chat";

/**
 * ロケットアイコンコンポーネント
 * ステータス: 進行中を表示
 */
const RocketIcon = () => (
  <svg 
    width="20" 
    height="20" 
    viewBox="0 0 24 24" 
    fill="none" 
    xmlns="http://www.w3.org/2000/svg" 
    className="status-svg"
  >
    {/* 1. ロケット本体：機首、胴体、翼を含む */}
    <path 
      d="M21 3C21 3 17.5 9.5 15.5 11.5C14.5 12.5 13.5 12.5 13.5 12.5L16 17.5L12 16L10.5 19L8.5 14L3 12.5L8 10.5C8 10.5 8 9.5 9 8.5C11 6.5 21 3 21 3Z" 
      stroke="currentColor" 
      strokeWidth="1.5" 
      strokeLinecap="round" 
      strokeLinejoin="round" 
    />

    {/* 2. 舷窓 */}
    <circle 
      cx="14.5" 
      cy="9.5" 
      r="1.5" 
      stroke="currentColor" 
      strokeWidth="1.5" 
    />

    {/* 3. 噴射気流：推進の動感を表現 */}
    <path d="M6.5 16.5L4 19" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
    <path d="M10 18L8.5 20.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
    <path d="M4.5 13.5L3 15" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
  </svg>
);

/**
 * チェックアイコンコンポーネント
 * ステータス: 完了を表示
 */
const CheckIcon = () => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" className="status-svg">
    <path d="M12 22C17.5 22 22 17.5 22 12C22 6.5 17.5 2 12 2C6.5 2 2 6.5 2 12C2 17.5 6.5 22 12 22Z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
    <path d="M7.75 12L10.58 14.83L16.25 9.17004" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
  </svg>
);

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
      <div className="report-header">📋 スクリーニング条件の提案</div>
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


// --- App メインコンポーネント ---
function App() {
  const [messages, setMessages] = useState([]); 
  const [userInput, setUserInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  
  // 状態: thinking (AI思考中), responding (テキスト返信中), executing (ツール実行中)
  const [aiState, setAiState] = useState('idle'); 

  const [currentLogMessages, setCurrentLogMessages] = useState([]); 
  const logRef = useRef(null); 
  const messagesEndRef = useRef(null); 
  const [previewImage, setPreviewImage] = useState(null);
  
  // セッション ID 管理
  const [sessionId, setSessionId] = useState(null);

  // Session ID の初期化
  useEffect(() => {
      if (!sessionId) {
          setSessionId(`sess_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`);
      }
      setMessages([
        { sender: AI_ASSISTANT_NAME, text: `こんにちは。産業チェーンの分析や、見込み顧客のリストアップをサポートします。具体的にどのような企業や業界をお探しですか？`, type: 'text' }
      ]);
  }, []);

  // 自動スクロール処理
  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [currentLogMessages]);

  useEffect(() => {
    if (messagesEndRef.current) messagesEndRef.current.scrollIntoView({ behavior: "smooth" });
  }, [messages, aiState]);

  const handleSendMessage = async () => {
    if (!userInput.trim()) return;

    const promptText = userInput;
    const userMessage = { sender: USER_NAME, text: promptText, type: 'text' };
    
    setMessages((prev) => [...prev, userMessage]);
    setUserInput(''); 
    setIsLoading(true);
    setAiState('thinking'); // 思考状態へ移行

    // 一時変数の準備
    let tempAiMsgId = Date.now();
    let isToolRunning = false;
    let incomingTextResponse = "";
    
    try {
      const response = await fetch(API_ENDPOINT, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ 
            message: promptText,
            session_id: sessionId 
        }),
      });

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
          const dataPrefix = 'data: ';
          
          if (event.startsWith(dataPrefix)) {
            const logLine = event.substring(dataPrefix.length).trim();
            
            if (logLine === "---END_OF_STREAM---") {
                break;
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

            // --- 特殊制御マーカーの処理 ---

            // 1. [Thinking] マーカー
            if (logLine.startsWith('[Thinking]')) {
                setAiState('thinking');
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
          setMessages(prev => [...prev, { sender: AI_ASSISTANT_NAME, text: "スクリーニング条件の生成が完了しました。上のレポートをご確認ください。条件の変更や追加の指示があれば、お知らせください。", type: 'text' }]);
      }

    } catch (error) {
      setMessages(prev => [...prev, { sender: AI_ASSISTANT_NAME, text: `エラーが発生しました: ${error.message}`, type: 'text' }]);
    } finally {
      setIsLoading(false);
      setAiState('idle');
    }
  };

  const handleKeyPress = (e) => {
    if (e.key === 'Enter' && !isLoading) handleSendMessage();
  };

  const closePreview = () => setPreviewImage(null);

  return (
    <div className="chat-container">
      {/* プレビューモーダル */}
      {previewImage && (
          <div className="image-modal-overlay" onClick={closePreview}>
              <div className="image-modal-content" onClick={(e) => e.stopPropagation()}>
                  <span className="close-button" onClick={closePreview}>&times;</span>
                  <img src={`data:image/png;base64,${previewImage}`} alt="Full Preview" />
              </div>
          </div>
      )}

      <div className="messages-area">
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
                  ) : (
                      /* 分岐2: 通常メッセージ (アバター + 吹き出し) */
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
                                        <span>{msg.text}</span>
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
                   <div className="message-bubble ai" style={{color: '#888', fontStyle: 'italic'}}>
                       考え中...
                   </div>
              </div>
          </div>
      )}
      
      <div ref={messagesEndRef} />
      </div>
      
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
        <button onClick={handleSendMessage} disabled={isLoading}>
            {isLoading ? '送信' : '送信'}
        </button>
      </div>
    </div>
  );
}

export default App;