import React, {useEffect, useMemo, useRef, useState} from "react";
import PlatformSelect, {PlatformMark} from "./PlatformSelect.jsx";

const BROWSER_CODES=new Set(["douyin","xiaohongshu","kuaishou","wechat_channels","bilibili","tiktok"]);
const API_CODES=new Set(["youtube","facebook","instagram","x"]);
const PROVIDER={youtube:"youtube",facebook:"facebook_page",instagram:"instagram_business",x:"x"};
const loginText={not_logged_in:"尚未登录",login_pending:"等待登录",login_required:"需要登录",starting:"正在启动",browser_open:"浏览器已打开",waiting_login:"等待登录",verifying:"正在确认",logged_in:"已登录",cancelled:"登录已取消",failed:"登录失败",expired:"登录已超时",check_failed:"检测失败",qr_unavailable:"二维码不可用",verification_required:"等待人工验证",network_error:"网络不可用",page_changed:"页面状态异常",needs_manual_action:"需要人工处理",not_applicable:"不适用"};
const authText={not_authorized:"尚未授权",not_configured:"尚未配置接入",config_invalid:"接入配置异常",ready_to_authorize:"等待账号授权",opening_browser:"正在打开浏览器",waiting_callback:"等待用户确认",reading_accounts:"正在读取账号",selecting_account:"请选择账号",authorized:"已授权",token_expired:"Token已过期",needs_reauthorization:"需要重新授权",authorization_cancelled:"授权已取消",authorization_failed:"授权失败"};

function platformText(item){return {douyin:"抖音",xiaohongshu:"小红书",kuaishou:"快手",wechat_channels:"视频号",bilibili:"B站",tiktok:"TikTok",youtube:"YouTube",facebook:"Facebook Page",instagram:"Instagram Business",x:"X"}[item?.code]||item?.name||"平台"}
function isApi(item){return API_CODES.has(item?.code)||item?.integration_type==="api"}
function apiDescription(code){return {youtube:"连接并管理你的 YouTube 频道",facebook:"连接你拥有管理权限的 Facebook 主页",instagram:"绑定符合 API 要求的 Instagram 专业账号",x:"使用 X 官方 OAuth 2.0 授权账号"}[code]||"使用官方 API 授权，不保存平台密码"}

export function OAuthStatusBadge({status}){
  const tone=status==="authorized"?"ok":["authorization_failed","config_invalid"].includes(status)?"error":["waiting_callback","reading_accounts","selecting_account","opening_browser"].includes(status)?"busy":"muted";
  return <span className={`oauth-status-badge ${tone}`}><i/>{authText[status]||"尚未授权"}</span>;
}

export function ApiProviderSetupDialog({platform,preflight,admin,request,notify,onClose,onSaved}){
  const provider=PROVIDER[platform.code];
  const [fields,setFields]=useState({client_id:"",client_secret:"",review_note:"",oauth_mode:"authorization_code_pkce"});
  const [busy,setBusy]=useState("");
  const callback=preflight?.callback_uri||"";
  const copy=async()=>{await navigator.clipboard.writeText(callback);notify("回调地址已复制")};
  const importGoogle=async()=>{try{setBusy("import");const result=await request("/api/select-google-oauth-client",{method:"POST",body:{}});notify(result.message||"Google OAuth配置已导入");await onSaved()}catch(error){notify(error.message)}finally{setBusy("")}};
  const save=async()=>{if(!fields.client_id.trim())return notify(platform.code==="facebook"||platform.code==="instagram"?"请填写应用编号":"请填写客户端编号");if(platform.code!=="x"&&!fields.client_secret.trim()&&!preflight?.secret_configured)return notify("请填写应用密钥");try{setBusy("save");await request(`/api/oauth/${provider}/config`,{method:"POST",body:fields});notify("平台开发者配置已安全保存");await onSaved()}catch(error){notify(error.message)}finally{setBusy("")}};
  return <div className="account-modal-backdrop" onMouseDown={onClose}><section className="oauth-setup-dialog" onMouseDown={e=>e.stopPropagation()}>
    <header><div><small>平台开发者配置</small><h2>{platformText(platform)} 接入配置</h2><p>{apiDescription(platform.code)}</p></div><button onClick={onClose}>×</button></header>
    {!admin?<div className="oauth-ordinary-guide"><b>当前平台尚未完成接入配置</b><p>请联系软件管理员完成平台开发者配置。普通用户无需填写应用密钥。</p></div>:platform.code==="youtube"?<div className="oauth-youtube-import"><ol><li>在 Google Cloud 创建桌面应用 OAuth 客户端</li><li>下载客户端 JSON 文件</li><li>点击下方按钮导入，软件会加密保存密钥</li></ol><div className="oauth-callback-row"><span>本机回调地址</span><code>{callback}</code><button onClick={copy}>复制</button></div><button className="oauth-primary" disabled={!!busy} onClick={importGoogle}>{busy?"正在导入":"导入 Google Desktop OAuth JSON"}</button></div>:<div className="oauth-config-form">
      <label><span>{platform.code==="facebook"||platform.code==="instagram"?"应用编号（App ID）":"客户端编号（Client ID）"}</span><input value={fields.client_id} onChange={e=>setFields({...fields,client_id:e.target.value})} placeholder="重新输入并覆盖保存"/></label>
      <label><span>{platform.code==="facebook"||platform.code==="instagram"?"应用密钥（App Secret）":"客户端密钥（Client Secret）"}</span><input type="password" value={fields.client_secret} onChange={e=>setFields({...fields,client_secret:e.target.value})} placeholder={preflight?.secret_configured?"已配置，留空则保持不变":"请输入平台开发者密钥"}/></label>
      {platform.code==="x"&&<p className="field-help">优先使用 OAuth 2.0 Authorization Code + PKCE。若应用为公开客户端，密钥可以留空。</p>}
      <div className="oauth-callback-row"><span>授权回调地址</span><code>{callback}</code><button onClick={copy}>复制</button></div>
      <label><span>平台审核说明</span><textarea value={fields.review_note} onChange={e=>setFields({...fields,review_note:e.target.value})} placeholder="可选，仅记录配置备注，不影响授权"/></label>
    </div>}
    <footer><button onClick={onClose}>取消</button>{admin&&platform.code!=="youtube"&&<button className="oauth-primary" disabled={!!busy} onClick={save}>{busy?"保存中":"保存配置"}</button>}</footer>
  </section></div>;
}

export function OAuthAccountSelector({session,request,notify,onClose,onComplete}){
  const [selected,setSelected]=useState(session.candidates?.[0]?.id||"");
  const [busy,setBusy]=useState(false);
  const bind=async()=>{if(!selected)return notify("请选择需要绑定的账号");try{setBusy(true);const result=await request(`/api/oauth/session/${session.state}/select`,{method:"POST",body:{external_id:selected}});notify(result.message||"账号授权已完成");onComplete(result)}catch(error){notify(error.message)}finally{setBusy(false)}};
  return <div className="account-modal-backdrop"><section className="oauth-account-selector"><header><div><small>授权账号选择</small><h2>选择要绑定的账号</h2><p>授权成功不代表发布已通过验收。</p></div></header><div className="oauth-candidate-list">{(session.candidates||[]).map(item=><button key={item.id} className={selected===item.id?"selected":""} onClick={()=>setSelected(item.id)}>{item.avatar?<img src={item.avatar}/>:<i>{(item.name||"账").slice(0,1)}</i>}<span><b>{item.name}</b><small>{item.kind} · {item.id?.slice(0,3)}***{item.id?.slice(-3)}</small></span><em>{selected===item.id?"已选择":"选择"}</em></button>)}{!session.candidates?.length&&<p>当前账号没有可绑定的主页、频道或专业账号。</p>}</div><footer><button onClick={onClose}>取消</button><button className="oauth-primary" disabled={busy||!selected} onClick={bind}>{busy?"正在绑定":"绑定此账号"}</button></footer></section></div>;
}

export function OAuthWaitingDialog({initial,request,notify,onClose,onComplete}){
  const [session,setSession]=useState(initial);
  const timer=useRef(null);
  useEffect(()=>{if(!session?.state)return;const poll=async()=>{try{const next=await request(`/api/oauth/session/${session.state}`);setSession(next);if(next.status==="authorized")onComplete(next);if(["selecting_account","authorization_failed","authorization_cancelled"].includes(next.status))clearInterval(timer.current)}catch(error){clearInterval(timer.current);notify(error.message)}};timer.current=setInterval(poll,1200);return()=>clearInterval(timer.current)},[session?.state]);
  const cancel=async()=>{try{await request(`/api/oauth/session/${session.state}/cancel`,{method:"POST",body:{}})}finally{onClose()}};
  const reopen=async()=>{const next=await request(`/api/oauth/session/${session.state}/reopen`,{method:"POST",body:{}});setSession(next);if(!next.browser_opened)notify("未能自动打开浏览器，请复制授权链接后手动打开")};
  const copy=async()=>{await navigator.clipboard.writeText(session.authorization_url||"");notify("授权链接已复制")};
  const step={opening_browser:2,waiting_callback:3,reading_accounts:4,selecting_account:5,authorized:5}[session.status]||1;
  if(session.status==="selecting_account")return <OAuthAccountSelector session={session} request={request} notify={notify} onClose={cancel} onComplete={onComplete}/>;
  return <div className="account-modal-backdrop"><section className="oauth-waiting-dialog"><header><span className="oauth-spinner"/><div><small>账号授权</small><h2>正在等待 {session.platform_name||"平台"} 授权</h2></div></header><ol>{["正在准备授权","已打开系统浏览器","等待用户确认","正在读取账号信息","授权完成"].map((label,index)=><li className={index+1<step?"done":index+1===step?"active":""} key={label}><i>{index+1<step?"✓":index+1}</i><span>{label}</span></li>)}</ol>{session.message&&<p className={session.status.includes("failed")?"oauth-error":"oauth-message"}>{session.message}</p>}<div className="oauth-wait-actions"><button onClick={reopen}>重新打开浏览器</button><button onClick={copy}>复制授权链接</button><button onClick={cancel}>取消授权</button></div></section></div>;
}

export function TikTokLoginDialog({account,request,notify,onClose,onComplete}){
  const [mode,setMode]=useState("qr");
  const [session,setSession]=useState(null);
  const [busy,setBusy]=useState(false);
  const timer=useRef(null);
  const active=session&&["opening_browser","waiting_login","login_pending","verification_required","qr_unavailable"].includes(session.status);
  const start=async nextMode=>{try{setBusy(true);setMode(nextMode);const result=await request(`/api/publish/accounts/${account.id}/login`,{method:"POST",body:{login_mode:nextMode}});setSession(result)}catch(error){notify(error.message)}finally{setBusy(false)}};
  useEffect(()=>{if(!active)return;const poll=async()=>{try{const next=await request(`/api/publish/accounts/${account.id}/login/status`,{method:"POST",body:{}});setSession(next);if(next.status==="logged_in"){clearInterval(timer.current);notify("TikTok账号登录成功");await onComplete(next)}}catch(error){clearInterval(timer.current);notify(error.message)}};timer.current=setInterval(poll,1000);return()=>clearInterval(timer.current)},[active,account.id]);
  const cancel=async()=>{try{if(session)await request(`/api/publish/accounts/${account.id}/login/cancel`,{method:"POST",body:{}})}finally{onClose()}};
  const statusTitle={opening_browser:"正在打开登录页面",waiting_login:"正在等待TikTok登录",login_pending:"正在等待TikTok登录",qr_unavailable:"二维码登录未成功",verification_required:"等待用户完成TikTok验证",network_error:"当前无法连接TikTok",page_changed:"TikTok页面状态异常",browser_closed:"登录窗口已关闭",logged_in:"TikTok账号已登录"}[session?.status]||"选择登录方式";
  return <div className="account-modal-backdrop" onMouseDown={cancel}><section className="tiktok-login-dialog" onMouseDown={event=>event.stopPropagation()}>
    <header><div><small>TikTok独立浏览器账号</small><h2>TikTok账号登录</h2><p>登录过程在TikTok官方页面完成，MixcutAI不会读取或保存你的密码。</p></div><button onClick={cancel}>×</button></header>
    <div className="tiktok-login-body">
      <div className="tiktok-login-methods">
        <button className={mode==="qr"?"selected":""} disabled={busy} onClick={()=>start("qr")}><b>扫码登录</b><span>使用TikTok手机端扫描官方二维码</span></button>
        <button className={mode==="account"?"selected":""} disabled={busy} onClick={()=>start("account")}><b>手机号 / 邮箱 / 用户名</b><span>在官方页面手动输入账号并完成验证</span></button>
        <button disabled><b>TikTok官方授权</b><span>TikTok官方API尚未配置</span></button>
      </div>
      <div className={`tiktok-login-state ${session?.status||"idle"}`}>
        {active&&!(["qr_unavailable","verification_required"].includes(session?.status))&&<span className="oauth-spinner"/>}
        <div><b>{statusTitle}</b><p>{session?.message||"请选择一种登录方式，然后在打开的TikTok官方页面中完成操作。"}</p></div>
      </div>
      {session?.status==="qr_unavailable"&&<div className="tiktok-login-fallback"><p>TikTok暂时无法通过当前二维码完成登录。</p><button className="oauth-primary" onClick={()=>start("account")}>尝试账号登录</button><button onClick={()=>start("qr")}>重新扫码</button></div>}
      {session?.status==="verification_required"&&<div className="tiktok-login-note">请回到TikTok窗口完成验证码、手机确认或安全验证，软件不会绕过这些验证。</div>}
      {session?.status==="network_error"&&<div className="tiktok-login-note error">请检查当前海外网络环境后重新打开登录页面。</div>}
      {session?.profile_path&&<div className="tiktok-login-meta"><span>独立环境</span><code title={session.profile_path}>{session.profile_path}</code><span>Chromium {session.browser_version||""}</span></div>}
    </div>
    <footer><button onClick={cancel}>取消</button>{session&&session.status!=="logged_in"&&<button disabled={busy} onClick={()=>request(`/api/publish/accounts/${account.id}/login/status`,{method:"POST",body:{check:true}}).then(next=>{setSession(next);if(next.status==="logged_in"){notify("TikTok账号登录成功");onComplete(next)}}).catch(error=>notify(error.message))}>检测登录状态</button>}{!session&&<button className="oauth-primary" disabled={busy} onClick={()=>start(mode)}>打开登录浏览器</button>}</footer>
  </section></div>;
}

export function BrowserPlatformLoginDialog({account,platform,request,notify,onClose,onComplete}){
  const [session,setSession]=useState(null);
  const [busy,setBusy]=useState(false);
  const timer=useRef(null);
  const started=useRef(false);
  const completed=useRef(false);
  const active=session&&["starting","browser_open","waiting_login","verifying","login_pending"].includes(session.status);
  const name=platformText(platform);
  const start=async(reopen=false)=>{try{setBusy(true);const result=await request(`/api/publish/accounts/${account.id}/login${reopen?"/reopen":""}`,{method:"POST",body:{}});setSession(result)}catch(error){notify(error.message)}finally{setBusy(false)}};
  useEffect(()=>{if(started.current)return;started.current=true;start(false)},[]);
  useEffect(()=>{if(!active)return;const poll=async()=>{try{const next=await request(`/api/publish/accounts/${account.id}/login/status`,{method:"POST",body:{}});setSession(next);if(next.status==="logged_in"&&!completed.current){completed.current=true;clearInterval(timer.current);notify(`${name}登录成功`);await onComplete(next)}}catch(error){clearInterval(timer.current);notify(error.message)}};poll();timer.current=setInterval(poll,2000);return()=>clearInterval(timer.current)},[active,account.id,name]);
  const cancel=async()=>{try{if(session&&active)await request(`/api/publish/accounts/${account.id}/login/cancel`,{method:"POST",body:{}})}finally{onClose()}};
  const reopen=async()=>{completed.current=false;await start(true)};
  const title={starting:"正在启动登录环境",browser_open:`${name}登录窗口已打开`,waiting_login:platform?.code==="wechat_channels"?"正在等待视频号扫码登录":"正在等待小红书登录",verifying:"正在确认登录状态",logged_in:`${name}账号已登录`,cancelled:"登录窗口已关闭",failed:"登录未完成",expired:"登录等待已超时"}[session?.status]||`正在打开${name}登录页面`;
  return <div className="account-modal-backdrop"><section className="tiktok-login-dialog domestic-login-dialog" onMouseDown={event=>event.stopPropagation()}>
    <header><div><small>独立浏览器账号</small><h2>{name}账号登录</h2><p>请在{name}官方页面完成登录，软件不会读取或保存密码、验证码和二维码。</p></div><button onClick={cancel}>×</button></header>
    <div className="tiktok-login-body">
      <div className={`tiktok-login-state ${session?.status||"starting"}`}>{(active||busy)&&<span className="oauth-spinner"/>}<div><b>{title}</b><p>{session?.message||`正在打开${name}官方登录页面，请稍候。`}</p></div></div>
      {session?.error_code==="NETWORK_ERROR"&&<div className="tiktok-login-note error">请检查网络后在登录窗口点击重试，窗口会继续保持打开。</div>}
      {session?.error_code==="PROFILE_LOCKED"&&<div className="tiktok-login-note">该账号环境已经在另一个窗口中使用，请先完成或关闭旧窗口。</div>}
      {session?.profile_path&&<div className="tiktok-login-meta"><span>独立环境</span><code title={session.profile_path}>{session.profile_path}</code>{session.browser_pid>0&&<span>进程 {session.browser_pid}</span>}</div>}
    </div>
    <footer><button onClick={cancel}>{active?"取消登录":"关闭"}</button>{session&&session.status!=="logged_in"&&!active&&<button className="oauth-primary" disabled={busy} onClick={reopen}>{busy?"正在打开":"重新打开登录窗口"}</button>}</footer>
  </section></div>;
}

export default function PublishAccountsPage({db,refresh,notify,Button,Icon,onTasks,onTests}){
  const request=window.__mixcutRequest;
  const platforms=(db.platforms||[]).filter(item=>item.enabled);
  const accounts=db.publish_accounts||[];
  const groups=db.publish_account_groups||[];
  const [platformId,setPlatformId]=useState(platforms[0]?.id||0);
  const [accountName,setAccountName]=useState("");
  const [busy,setBusy]=useState("");
  const [setup,setSetup]=useState(null);
  const [oauth,setOauth]=useState(null);
  const [tiktokLogin,setTiktokLogin]=useState(null);
  const [browserLogin,setBrowserLogin]=useState(null);
  const [filterPlatform,setFilterPlatform]=useState("all");
  const [filterState,setFilterState]=useState("all");
  const [query,setQuery]=useState("");
  const [groupDialog,setGroupDialog]=useState(null);
  const [selectedPreflight,setSelectedPreflight]=useState(null);
  const selected=platforms.find(item=>Number(item.id)===Number(platformId));
  const selectedAccounts=accounts.filter(item=>Number(item.platform_id)===Number(platformId));
  const connected=accounts.filter(item=>item.login_status==="logged_in"||item.authorization_status==="authorized").length;
  const filtered=useMemo(()=>accounts.filter(item=>{
    const state=isApi(item)?item.authorization_status:item.login_status;
    return (filterPlatform==="all"||Number(item.platform_id)===Number(filterPlatform))&&(filterState==="all"||state===filterState)&&(!query.trim()||`${item.account_name} ${item.channel_title} ${item.platform_name}`.toLowerCase().includes(query.toLowerCase()));
  }),[accounts,filterPlatform,filterState,query]);

  useEffect(()=>{let alive=true;if(!selected||!isApi(selected)){setSelectedPreflight(null);return}request(`/api/oauth/${PROVIDER[selected.code]}/preflight`).then(value=>{if(alive)setSelectedPreflight(value)}).catch(()=>{if(alive)setSelectedPreflight(null)});return()=>{alive=false}},[selected?.code,accounts.length]);

  const preflight=async(platform,accountId=0)=>request(`/api/oauth/${PROVIDER[platform.code]}/preflight${accountId?`?account_id=${accountId}`:""}`);
  const openSetup=async platform=>{try{setBusy("preflight");setSetup({platform,preflight:await preflight(platform)})}catch(error){notify(error.message)}finally{setBusy("")}};
  const beginOAuth=async account=>{const platform=platforms.find(item=>Number(item.id)===Number(account.platform_id));try{setBusy(`${account.id}-login`);const check=await preflight(platform,account.id);if(!check.developer_configured){setSetup({platform,preflight:check});return}const result=await request(`/api/oauth/${PROVIDER[platform.code]}/start`,{method:"POST",body:{account_id:account.id}});setOauth({...result,platform_name:platformText(platform)});if(!result.browser_opened)notify("未能自动打开浏览器，请复制授权链接后手动打开")}catch(error){notify(error.message)}finally{setBusy("")}};
  const add=async()=>{if(!selected||!accountName.trim())return notify("请输入账号备注名称");try{setBusy("add");if(isApi(selected)){const check=await preflight(selected);if(!check.developer_configured){setSetup({platform:selected,preflight:check});return}}const result=await request("/api/publish/accounts",{method:"POST",body:{platform_id:Number(selected.id),account_name:accountName.trim()}});const created={id:result.id,platform_id:selected.id,account_name:accountName.trim()};setAccountName("");await refresh(false);if(isApi(selected))await beginOAuth(created);else if(selected.code==="tiktok")setTiktokLogin(created);else if(["xiaohongshu","wechat_channels"].includes(selected.code))setBrowserLogin({account:created,platform:selected});else{await request(`/api/publish/accounts/${result.id}/login`,{method:"POST",body:{}});notify("浏览器环境已创建，请完成账号登录")}}catch(error){notify(error.message)}finally{setBusy("")}};
  const loginBrowserAccount=account=>{const platform=platforms.find(item=>Number(item.id)===Number(account.platform_id));if(platform?.code==="tiktok")setTiktokLogin(account);else if(["xiaohongshu","wechat_channels"].includes(platform?.code))setBrowserLogin({account,platform});else request(`/api/publish/accounts/${account.id}/login`,{method:"POST",body:{}}).then(()=>notify("登录窗口已打开，请在平台官方页面完成登录")).catch(error=>notify(error.message))};
  const check=async account=>{try{setBusy(`${account.id}-check`);const result=await request(`/api/publish/accounts/${account.id}/check`,{method:"POST",body:{}});notify(result.message||"检测完成");await refresh(false)}catch(error){notify(error.message)}finally{setBusy("")}};
  const remove=async account=>{if(!confirm(`确定删除账号“${account.account_name}”？`))return;await request(`/api/publish/accounts/${account.id}`,{method:"DELETE"});notify("账号及本地授权Token已清理");await refresh(false)};

  const openGroup=group=>setGroupDialog({id:group?.id||0,name:group?.name||"",description:group?.description||"",accountIds:(group?.members||[]).map(item=>Number(item.account_id))});
  const saveGroup=async()=>{if(!groupDialog.name.trim())return notify("分组名称不能为空");await request(groupDialog.id?`/api/publish/groups/${groupDialog.id}`:"/api/publish/groups",{method:"POST",body:{name:groupDialog.name,description:groupDialog.description,account_ids:groupDialog.accountIds}});setGroupDialog(null);await refresh(false);notify("账号分组已保存")};

  return <div className="page publish-account-page account-access-workspace">
    <div className="page-header"><div><span>发布账号</span><h1>账号管理</h1><p>浏览器平台保存独立登录环境，API平台使用官方OAuth授权。</p></div><div><Button quiet onClick={onTasks}>返回发布任务</Button>{db.admin_mode&&<Button quiet onClick={onTests}>平台发布测试</Button>}</div></div>
    <section className="account-status-strip"><div><Icon name="grid" size={17}/><span>已启用平台<small>浏览器与API分离管理</small></span><strong>{platforms.length}</strong></div><div><Icon name="user" size={17}/><span>账号环境<small>每个账号独立保存</small></span><strong>{accounts.length}</strong></div><div><Icon name="check" size={17}/><span>已连接账号<small>登录或授权有效</small></span><strong>{connected}</strong></div></section>
    <section className="account-connect-panel"><div className="account-section-heading"><div><h2>新增发布账号</h2><p>选择平台后只显示该平台需要的操作。</p></div></div><div className="platform-access-picker"><label><span>选择平台</span><PlatformSelect platforms={platforms} value={platformId} onChange={id=>setPlatformId(Number(id))}/></label><label className="account-name-field"><span>账号备注名称</span><input value={accountName} onChange={e=>setAccountName(e.target.value)} placeholder="例如：品牌主账号"/></label><Button primary disabled={busy==="add"} onClick={add}>{busy==="add"?"处理中":isApi(selected)?"创建并授权":"创建浏览器环境"}</Button></div>
      {selected&&<div className="platform-detail-panel"><div className="platform-detail-title"><PlatformMark platform={selected} size={30}/><span><b>{platformText(selected)}</b><small>{isApi(selected)?apiDescription(selected.code):"使用独立浏览器环境保存登录状态"}</small></span></div><div className="platform-detail-fact"><span>接入方式</span><b>{isApi(selected)?"官方API":"独立浏览器"}</b></div><div className="platform-detail-fact"><span>开发者配置</span><b>{isApi(selected)?selectedPreflight?.developer_configured?"已完成":"尚未配置":"无需配置"}</b></div><div className="platform-detail-fact"><span>账号状态</span><b>{isApi(selected)?selectedPreflight?.authorized_account_count?"可使用":"需要授权":selectedAccounts.some(a=>a.login_status==="logged_in")?"可使用":"需要登录"}</b></div><div className="platform-detail-actions">{isApi(selected)&&db.admin_mode&&<Button quiet onClick={()=>openSetup(selected)}>配置平台接入</Button>}{isApi(selected)&&selectedAccounts[0]&&<Button quiet onClick={()=>beginOAuth(selectedAccounts[0])}>授权账号</Button>}</div></div>}
    </section>
    <section className="account-resource-panel"><div className="account-section-heading"><div><h2>账号连接状态</h2><p>浏览器平台检查真实登录状态，API平台检查官方授权状态。</p></div><span>{filtered.length}/{accounts.length} 个账号</span></div><div className="account-filter-toolbar"><label><span>平台</span><PlatformSelect platforms={platforms} value={filterPlatform} includeAll compact onChange={setFilterPlatform}/></label><label><span>状态</span><select value={filterState} onChange={e=>setFilterState(e.target.value)}><option value="all">全部状态</option><option value="authorized">已授权</option><option value="token_expired">Token已过期</option><option value="logged_in">已登录</option><option value="not_authorized">尚未授权</option></select></label><label><span>搜索</span><input value={query} onChange={e=>setQuery(e.target.value)} placeholder="输入账号名称"/></label></div>
      <div className="account-resource-table">{filtered.length>0&&<div className="account-table-head"><span>平台与账号</span><span>接入方式</span><span>登录/授权状态</span><span>最近检测</span><span>操作</span></div>}{filtered.map(account=>{const platform=platforms.find(item=>Number(item.id)===Number(account.platform_id));const api=isApi(account)||isApi(platform);return <article key={account.id}><div className="account-identity"><PlatformMark platform={platform} size={26}/><span><b>{account.channel_title||account.account_name}</b><small>{platformText(platform)}{account.channel_id?` · ${account.channel_id.slice(0,3)}***${account.channel_id.slice(-3)}`:""}</small></span></div><div className="account-method"><b>{api?"官方API授权":"独立浏览器"}</b><small>{account.region==="overseas"?"需要海外网络":"国内网络"}</small></div><div className="account-state">{api?<OAuthStatusBadge status={account.authorization_status}/>:<span>{loginText[account.login_status]||"尚未登录"}</span>}<small>{api&&!account.api_configured?"开发者配置尚未完成":""}</small></div><div className="account-last-check"><b>{account.last_checked_at||"尚未检测"}</b><small>{api?"授权状态检测":"登录状态检测"}</small></div><div className="account-row-actions"><Button quiet disabled={busy===`${account.id}-login`} onClick={()=>api?beginOAuth(account):loginBrowserAccount(account)}>{api?account.authorization_status==="authorized"?"重新授权":"授权登录":account.login_status==="logged_in"?"重新登录":"登录账号"}</Button><Button quiet disabled={busy===`${account.id}-check`} onClick={()=>check(account)}>检测</Button>{!api&&<button title="打开浏览器目录" onClick={()=>request(`/api/publish/accounts/${account.id}/open-profile`,{method:"POST",body:{}})}><Icon name="folder" size={15}/></button>}<button className="danger" title="删除账号" onClick={()=>remove(account)}><Icon name="trash" size={15}/></button></div></article>})}{!filtered.length&&<div className="account-resource-empty"><Icon name="user" size={22}/><div><b>暂无符合条件的账号</b><span>请添加账号或调整筛选条件。</span></div></div>}</div>
    </section>
    <section className="account-groups-panel"><div className="account-section-heading"><div><h2>账号分组</h2><p>把多个平台账号组合为发布矩阵。</p></div><Button primary onClick={()=>openGroup(null)}>新建分组</Button></div><div className="account-groups-list">{groups.map(group=><article className="account-group-row" key={group.id}><div className="account-group-main"><strong>{group.name}</strong><small>{group.description||"未填写备注"}</small><span>{group.account_count||0} 个账号</span></div><div className="account-group-stats"><b>可发布 {group.publishable_count||0}</b><span>需要登录 {group.needs_login_count||0}</span><span>需要授权 {group.needs_auth_count||0}</span></div><div className="account-group-actions"><Button quiet onClick={()=>openGroup(group)}>编辑</Button><Button quiet onClick={async()=>{await request(`/api/publish/groups/${group.id}/copy`,{method:"POST",body:{}});await refresh(false)}}>复制</Button><button className="danger" onClick={async()=>{if(!confirm(`确定删除分组“${group.name}”？`))return;await request(`/api/publish/groups/${group.id}`,{method:"DELETE"});await refresh(false)}}>删除</button></div></article>)}{!groups.length&&<div className="account-resource-empty"><Icon name="grid" size={22}/><div><b>还没有账号分组</b><span>创建分组后可快速选择多个平台账号。</span></div></div>}</div></section>
    {setup&&<ApiProviderSetupDialog platform={setup.platform} preflight={setup.preflight} admin={db.admin_mode} request={request} notify={notify} onClose={()=>setSetup(null)} onSaved={async()=>{const next=await preflight(setup.platform);setSetup({...setup,preflight:next});await refresh(false)}}/>}
    {oauth&&<OAuthWaitingDialog initial={oauth} request={request} notify={notify} onClose={()=>setOauth(null)} onComplete={async()=>{setOauth(null);await refresh(false)}}/>}
    {tiktokLogin&&<TikTokLoginDialog account={tiktokLogin} request={request} notify={notify} onClose={()=>setTiktokLogin(null)} onComplete={async()=>{setTiktokLogin(null);await refresh(false)}}/>}
    {browserLogin&&<BrowserPlatformLoginDialog account={browserLogin.account} platform={browserLogin.platform} request={request} notify={notify} onClose={()=>setBrowserLogin(null)} onComplete={async()=>{setBrowserLogin(null);await refresh(false)}}/>}
    {groupDialog&&<div className="account-modal-backdrop" onMouseDown={()=>setGroupDialog(null)}><section className="account-admin-modal group-editor-modal" onMouseDown={e=>e.stopPropagation()}><header><div><small>发布矩阵</small><h2>{groupDialog.id?"编辑账号分组":"新建账号分组"}</h2></div><button onClick={()=>setGroupDialog(null)}>×</button></header><div className="group-fields"><label>分组名称<input value={groupDialog.name} onChange={e=>setGroupDialog({...groupDialog,name:e.target.value})}/></label><label>分组备注<input value={groupDialog.description} onChange={e=>setGroupDialog({...groupDialog,description:e.target.value})}/></label></div><div className="group-account-options">{accounts.map(account=><label key={account.id}><input type="checkbox" checked={groupDialog.accountIds.includes(Number(account.id))} onChange={()=>setGroupDialog({...groupDialog,accountIds:groupDialog.accountIds.includes(Number(account.id))?groupDialog.accountIds.filter(id=>id!==Number(account.id)):[...groupDialog.accountIds,Number(account.id)]})}/><span><b>{account.platform_name} · {account.account_name}</b><small>{isApi(account)?authText[account.authorization_status]||"尚未授权":loginText[account.login_status]||"尚未登录"}</small></span></label>)}</div><footer><Button quiet onClick={()=>setGroupDialog(null)}>取消</Button><Button primary onClick={saveGroup}>保存分组</Button></footer></section></div>}
  </div>;
}
