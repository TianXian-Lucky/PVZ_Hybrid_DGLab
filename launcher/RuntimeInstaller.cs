// C# 5, .NET Framework 4.7+. All runtime inputs are locked; no pip/npm installers.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;

internal static class RuntimeInstaller
{
    public sealed class Dep { public string kind {get;set;} public string file {get;set;} public string url {get;set;} public string sha256 {get;set;} }
    public sealed class Payload { public string path {get;set;} public string sha256 {get;set;} }
    public sealed class Plan { public int schema {get;set;} public string version {get;set;} public Dep[] dependencies {get;set;} public Payload[] payload {get;set;} }
    const string Rev = "pvz-github-win64-installer-1\n";
    const string Rt = "resources/backend/runtime/";
    const string Pth = "python313.zip\r\n.\r\nLib/site-packages\r\n../app/\r\n../app/core\r\n";
    static readonly StringComparer CI = StringComparer.OrdinalIgnoreCase;
    static readonly UTF8Encoding Utf8 = new UTF8Encoding(false, true);
    static readonly Regex Hex = new Regex(@"\A[0-9a-fA-F]{64}\z");
    // Keep offline validation independent of a writable install receipt for critical executables.
    static readonly Dictionary<string,string> Pin = new Dictionary<string,string>(StringComparer.OrdinalIgnoreCase) {
        {"electron.exe","a547b53fbee4dd4adca1a88e215270d06f7cb1e7d333ee402fb3dd677392a87a"},
        {Rt+"python.exe","7c2fc2b303a4ee3cc14cc17b47bc6e8bca98c78792edd123b846a560da44b203"},
        {Rt+"python313.dll","26026530507a7389e7892ce130e7c1753b32a2df03ec3721d45ad8c1705d40eb"},
        {Rt+"python313.zip","58af5bf33dcfdedf72f60b0926fdb619cc2960c9d4200677531d35f626c8ee24"} };
    static InvalidDataException Bad(string msg) { return new InvalidDataException(msg); }
    static void Say(Action<string> cb,string msg) { if (cb!=null) cb(msg); }
    static string Hash(Stream input) {
        using (var h=SHA256.Create()) return BitConverter.ToString(h.ComputeHash(input)).Replace("-","").ToLowerInvariant();
    }
    static string HashBytes(byte[] bytes) { using(var m=new MemoryStream(bytes,false)) return Hash(m); }
    static void NoLink(string path) {
        if ((File.Exists(path)||Directory.Exists(path)) && (File.GetAttributes(path)&FileAttributes.ReparsePoint)!=0)
            throw Bad("不使用链接或重解析点："+path);
    }
    static string Safe(string root,string rel) {
        if (String.IsNullOrEmpty(rel)||rel.Length>500||rel.IndexOf('\\')>=0||rel.StartsWith("/")) throw Bad("文件路径无效");
        string p=Path.GetFullPath(root);
        NoLink(p);
        foreach (string s in rel.Split('/')) {
            if (s.Length==0||s=="."||s==".."||s.EndsWith(" ")||s.EndsWith(".")||s.IndexOfAny(Path.GetInvalidFileNameChars())>=0 ||
                Regex.IsMatch(s.Split('.')[0], @"\A(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])\z", RegexOptions.IgnoreCase))
                throw Bad("不允许的文件路径："+rel);
            p=Path.Combine(p,s); NoLink(p);
        }
        return p;
    }
    static FileStream Read(string path) { return new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.Read,131072); }
    static string DigestFile(string p) { using(var f=Read(p)) return Hash(f); }
    static byte[] ReadSmall(string path,int max) {
        using(var f=Read(path)) {
            if(f.Length<=0||f.Length>max) throw Bad("文件大小不正确："+Path.GetFileName(path));
            using(var m=new MemoryStream()){f.CopyTo(m);return m.ToArray();}
        }
    }
    static void CheckPlan(Plan p,string repo) {
        if(p==null||p.schema!=1||String.IsNullOrEmpty(p.version)||p.dependencies==null||p.dependencies.Length<3||p.dependencies.Length>64||
            p.payload==null||p.payload.Length<5||p.payload.Length>512) throw Bad("启动清单不完整");
        var files=new HashSet<string>(CI);int e=0,py=0;
        foreach(var d in p.dependencies){
            if(d==null||String.IsNullOrEmpty(d.file)||!Regex.IsMatch(d.file,@"\A[A-Za-z0-9][A-Za-z0-9.+_-]{0,160}\z")||
               !files.Add(d.file)||d.sha256==null||!Hex.IsMatch(d.sha256)) throw Bad("依赖文件或校验值无效");
            Uri u;
            if(!Uri.TryCreate(d.url,UriKind.Absolute,out u)||!Secure(u)||u.Query!=""||u.Fragment!=""||
               !u.AbsolutePath.EndsWith("/"+d.file,StringComparison.Ordinal)) throw Bad("依赖地址无效："+d.file);
            if(d.kind=="electron"&&u.Host=="github.com"&&u.AbsolutePath.StartsWith("/electron/electron/releases/download/",StringComparison.Ordinal)&&
               d.file.EndsWith("-win32-x64.zip",StringComparison.Ordinal)) e++;
            else if(d.kind=="python"&&u.Host=="www.python.org"&&u.AbsolutePath.StartsWith("/ftp/python/",StringComparison.Ordinal)&&
               d.file.EndsWith("-embed-amd64.zip",StringComparison.Ordinal)) py++;
            else if(d.kind!="wheel"||u.Host!="files.pythonhosted.org"||!u.AbsolutePath.StartsWith("/packages/",StringComparison.Ordinal)||
               !d.file.EndsWith(".whl",StringComparison.Ordinal)) throw Bad("未授权的运行时来源："+d.file);
        }
        if(e!=1||py!=1) throw Bad("需要一个 Electron 和一个 Python 运行时");
        files.Clear();
        foreach(var t in p.payload){
            if(t==null||t.path==null||t.sha256==null||!Hex.IsMatch(t.sha256)||!files.Add(t.path)||
              !(t.path.StartsWith("resources/app/",StringComparison.Ordinal)||t.path.StartsWith("resources/backend/app/",StringComparison.Ordinal)||
                t.path.StartsWith("LICENSES/",StringComparison.Ordinal))) throw Bad("应用清单路径无效");
            Safe(repo,"app/"+t.path);
        }
        foreach(string k in new[]{"resources/app/package.json","resources/app/main.cjs","resources/backend/app/app.py","resources/backend/app/CORE-SHA256.txt"})
            if(!files.Contains(k)) throw Bad("下载包缺少："+k);
    }
    static bool Secure(Uri u) { return u!=null&&u.IsAbsoluteUri&&u.Scheme=="https"&&u.IsDefaultPort&&u.UserInfo==""; }
    static bool HostOK(Dep d,Uri u) {
        if(!Secure(u)) return false;
        if(d.kind=="electron") return u.Host=="github.com"||u.Host=="release-assets.githubusercontent.com"||
           u.Host=="objects.githubusercontent.com"||u.Host=="github-releases.githubusercontent.com";
        return d.kind=="wheel"?u.Host=="files.pythonhosted.org":u.Host=="www.python.org";
    }
    static void Fetch(Dep d,string file,Action<string> cb) {
        Uri uri=new Uri(d.url); long max=d.kind=="electron"?280L*1024*1024:110L*1024*1024;
        Stopwatch total=Stopwatch.StartNew();
        for(int hops=0;hops<=5;hops++) {
            if(!HostOK(d,uri)) throw Bad("拒绝非官方的下载重定向："+d.file);
            var req=(HttpWebRequest)WebRequest.Create(uri);
            req.AllowAutoRedirect=false;req.Timeout=35000;req.ReadWriteTimeout=35000;req.UserAgent="PVZ-Feedback-Portable/1";req.KeepAlive=false;
            using(var res=(HttpWebResponse)req.GetResponse()){
                int code=(int)res.StatusCode;
                if(code>=300&&code<400) {
                    string next=res.Headers["Location"];
                    if(String.IsNullOrEmpty(next)||!Uri.TryCreate(uri,next,out uri)) throw Bad("重定向地址无效");
                    continue;
                }
                if(code!=200||res.ContentLength>max) throw Bad("下载响应大小或状态无效："+d.file);
                using(var input=res.GetResponseStream())
                using(var output=new FileStream(file,FileMode.CreateNew,FileAccess.Write,FileShare.None,131072)){
                    byte[] buf=new byte[131072];long got=0;int count;long last=-10;
                    while((count=input.Read(buf,0,buf.Length))!=0){
                        got+=count;if(got>max||total.Elapsed.TotalMinutes>12) throw new IOException("下载超时或内容超限");
                        output.Write(buf,0,count);
                        if(res.ContentLength>4*1024*1024){long pct=100*got/res.ContentLength;if(pct>=last+10){last=pct;Say(cb,"正在下载 "+d.file+" · "+pct+"%");}}
                    }
                    if(got==0||(res.ContentLength>=0&&got!=res.ContentLength)) throw new IOException("下载未完成");
                }
                return;
            }
        }
        throw Bad("下载重定向次数过多");
    }
    static string Get(Dep d,string dir,Action<string> cb) {
        string path=Safe(dir,d.file);
        if(File.Exists(path)) {
            if(CI.Equals(DigestFile(path),d.sha256)) return path;
            File.Move(path,path+".bad-"+Guid.NewGuid().ToString("N").Substring(0,8));
        }
        Exception last=null;
        for(int i=0;i<2;i++){
            string tmp=path+".part-"+Guid.NewGuid().ToString("N");
            try{
                Say(cb,(i==0?"正在下载 ":"重新下载 ")+d.file);
                Fetch(d,tmp,cb);
                if(!CI.Equals(DigestFile(tmp),d.sha256)) throw Bad("下载文件 SHA-256 不符");
                File.Move(tmp,path);return path;
            }catch(Exception ex) { last=ex;if(i==0)Thread.Sleep(600); }
            finally{try{if(File.Exists(tmp)) File.Delete(tmp);}catch(IOException){}}
        }
        throw new IOException("不能取得 "+d.file+"。请检查网络后重新打开；已验证的其他文件会保留。"+last.Message,last);
    }
    // Lock is released by the OS if the bootstrap process terminates. Covers concurrent sessions.
    static FileStream Gate(string root,Action<string> cb) {
        string path=Safe(root,"installer.lock");Stopwatch timer=Stopwatch.StartNew();bool announced=false;
        while(true){
            try{return new FileStream(path,FileMode.OpenOrCreate,FileAccess.ReadWrite,FileShare.None);}
            catch(IOException){if(timer.Elapsed.TotalMinutes>15) throw new IOException("其他启动器仍在准备，请稍后重试");
                if(!announced){Say(cb,"正在等待其他启动器完成准备…");announced=true;}Thread.Sleep(350);}
        }
    }
    static void MkParent(string path) { Directory.CreateDirectory(Path.GetDirectoryName(path)); }
    static void Extract(Dep d,string source,string stage,Dictionary<string,long> sizes){
        long raw=0;
        using(var file=Read(source)) {
            if(!CI.Equals(Hash(file),d.sha256)) throw Bad("正在使用的安装包校验失败："+d.file);
            file.Position=0;
            using(var z=new ZipArchive(file,ZipArchiveMode.Read,true)){
                if(z.Entries.Count>18000)throw Bad("压缩包条目过多");
                foreach(var ent in z.Entries){
                    string n=ent.FullName;
                    if(n.EndsWith("/",StringComparison.Ordinal)) { Safe(stage,n.Substring(0,n.Length-1)); continue; }
                    if(String.IsNullOrEmpty(n))throw Bad("空的压缩包条目");
                    string rel;
                    if(d.kind=="electron"){
                        if(n=="resources/default_app.asar") continue;
                        rel=n=="LICENSE"?"LICENSES/Electron-LICENSE.txt":n=="LICENSES.chromium.html"?"LICENSES/Chromium-licenses.html":n;
                    }else if(d.kind=="python"){
                        if(n=="python313._pth"||n=="python.cat")continue;
                        rel=Rt+n;
                    }else{
                        if(n.IndexOf(".data/",StringComparison.Ordinal)>=0) throw Bad("此运行包需要其他安装规则");
                        rel=Rt+"Lib/site-packages/"+n;
                    }
                    string target=Safe(stage,rel);
                    if(ent.Length<0 || ent.Length>400L*1024*1024 || (raw+=ent.Length)>1200L*1024*1024)throw Bad("压缩包超出解包上限");
                    MkParent(target);
                    using(var input=ent.Open())
                    using(var outp=new FileStream(target,FileMode.CreateNew,FileAccess.Write,FileShare.None,131072)) {
                        byte[] buf=new byte[65536]; long written=0;int count;
                        while((count=input.Read(buf,0,buf.Length))>0){
                            if((written+=count)>ent.Length)throw Bad("压缩包条目长度不正确");
                            outp.Write(buf,0,count);
                        }
                        if(written!=ent.Length)throw Bad("压缩包内容不完整");
                    }
                    sizes.Add(rel,ent.Length);
                }
            }
        }
    }
    static void CopyApp(Plan plan,string repo,string stage) {
        foreach(var p in plan.payload){
            string target=Safe(stage,p.path);MkParent(target);
            using(var f=Read(Safe(repo,"app/"+p.path))){
                if(f.Length>32L*1024*1024||!CI.Equals(Hash(f),p.sha256))throw Bad("应用文件不完整："+p.path);
                f.Position=0;
                using(var o=new FileStream(target,FileMode.CreateNew,FileAccess.Write,FileShare.None)) f.CopyTo(o);
            }
        }
    }
    static void Verify(string root, Plan plan, string key) {
        var ser=new JavaScriptSerializer();
        var sizes=ser.Deserialize<Dictionary<string,long>>(Utf8.GetString(ReadSmall(Safe(root,".runtime-files.json"),2*1024*1024)));
        if(sizes==null || sizes.Count<60 || sizes.Count>18000 || File.ReadAllText(Safe(root,".install-key"),Utf8)!=key)throw Bad("安装记录无效");
        foreach(var rec in sizes){
            var f=new FileInfo(Safe(root,rec.Key));if(!f.Exists||f.Length!=rec.Value)throw Bad("运行时文件缺失或损坏："+rec.Key);
        }
        foreach(var known in Pin){
            string fp=Safe(root,known.Key);if(!CI.Equals(DigestFile(fp),known.Value))throw Bad("关键运行时文件校验失败："+known.Key);
        }
        if(File.ReadAllText(Safe(root,Rt+"python313._pth"),Encoding.ASCII)!=Pth)throw Bad("Python 搜索路径异常");
        foreach(var app in plan.payload)if(!CI.Equals(DigestFile(Safe(root,app.path)),app.sha256))throw Bad("应用文件校验失败："+app.path);
        if(File.Exists(Safe(root,"resources/app.asar"))||File.Exists(Safe(root,"resources/default_app.asar")))throw Bad("发现冲突的 Electron 应用入口");
    }
    public static string Ensure(string repoRoot,Action<string> progress) {
        if(!Environment.Is64BitOperatingSystem)throw new PlatformNotSupportedException("需要 64 位 Windows");
        string repo=Path.GetFullPath(repoRoot);
        byte[] json=ReadSmall(Safe(repo,"launcher/runtime.json"),2*1024*1024);
        var ser=new JavaScriptSerializer();var plan=ser.Deserialize<Plan>(Utf8.GetString(json).TrimStart('\uFEFF'));
        CheckPlan(plan,repo);
        // Include exact manifest bytes and installer revision, not user-dependent filesystem paths.
        byte[] prefix=Utf8.GetBytes(Rev), all=new byte[prefix.Length+json.Length];
        Buffer.BlockCopy(prefix,0,all,0,prefix.Length);Buffer.BlockCopy(json,0,all,prefix.Length,json.Length);
        string key="v-"+HashBytes(all).Substring(0,24);
        string local=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if(String.IsNullOrEmpty(local))throw Bad("无法确定本机应用数据目录");
        string home=Safe(local,"PVZFeedback-GitHub");
        Directory.CreateDirectory(home);
        using(Gate(home,progress)){
            Say(progress,"正在核对应用文件…");
            foreach(var p in plan.payload){
                string from=Safe(repo,"app/"+p.path);var info=new FileInfo(from);
                if(!info.Exists||info.Length>32L*1024*1024||!CI.Equals(DigestFile(from),p.sha256))throw Bad("应用文件不完整，请重新完整解压仓库："+p.path);
            }
            string installed=Safe(home,key);
            if(Directory.Exists(installed)){
                try{Verify(installed,plan,key);}
                catch(Exception ex){throw new IOException("本机缓存校验失败，请关闭程序并备份、移开这个目录后重试："+installed+"。"+ex.Message,ex);}
                return Safe(installed,"electron.exe");
            }
            string downloads=Safe(home,"downloads");Directory.CreateDirectory(downloads);
            ServicePointManager.SecurityProtocol=SecurityProtocolType.Tls12;
            var archives=new List<string>();
            for(int i=0;i<plan.dependencies.Length;i++){
                var d=plan.dependencies[i];Say(progress,"正在准备运行组件 "+(i+1)+"/"+plan.dependencies.Length+" · "+d.file);
                archives.Add(Get(d,downloads,progress));
            }
            var drive=new DriveInfo(Path.GetPathRoot(home));
            if(drive.AvailableFreeSpace<650L*1024*1024)throw new IOException("安装运行组件需要至少 650 MiB 可用磁盘空间");
            string stage=Safe(home,".prepare-"+Guid.NewGuid().ToString("N"));Directory.CreateDirectory(stage);
            var sizes=new Dictionary<string,long>(CI);
            for(int i=0;i<plan.dependencies.Length;i++){
                Say(progress,"正在解包 "+(i+1)+"/"+plan.dependencies.Length+" · "+plan.dependencies[i].file);
                Extract(plan.dependencies[i],archives[i],stage,sizes);
            }
            File.WriteAllText(Safe(stage,Rt+"python313._pth"),Pth,Encoding.ASCII);
            CopyApp(plan,repo,stage);
            File.WriteAllText(Safe(stage,".runtime-files.json"),ser.Serialize(sizes),Utf8);
            File.WriteAllText(Safe(stage,".install-key"),key,Utf8);
            Say(progress,"正在进行最后校验…");Verify(stage,plan,key);
            // Same volume, under the same locked private root; never replace an existing install.
            Directory.Move(stage,installed);
            return Safe(installed,"electron.exe");
        }
    }
}
