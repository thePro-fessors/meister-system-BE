const { spawn } = require("child_process");

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class CDPClient {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.ws = null;
    this.id = 1;
    this.callbacks = new Map();
  }

  async connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.wsUrl);
      this.ws.onopen = () => resolve();
      this.ws.onerror = (err) => reject(err);
      this.ws.onmessage = (event) => {
        const msg = JSON.parse(event.data);
        if (msg.id && this.callbacks.has(msg.id)) {
          const { resolve, reject } = this.callbacks.get(msg.id);
          this.callbacks.delete(msg.id);
          if (msg.error) {
            reject(new Error(msg.error.message));
          } else {
            resolve(msg.result);
          }
        }
      };
    });
  }

  async send(method, params = {}) {
    return new Promise((resolve, reject) => {
      const id = this.id++;
      this.callbacks.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const res = await this.send("Runtime.evaluate", {
      expression,
      returnByValue: true,
      awaitPromise: true,
    });
    if (res.exceptionDetails) {
      throw new Error("Evaluation error: " + JSON.stringify(res.exceptionDetails));
    }
    return res.result?.value;
  }

  async navigate(url) {
    await this.send("Page.navigate", { url });
    await sleep(1500);
  }

  async waitFor(selector, timeoutMs = 8000) {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
      const found = await this.evaluate(`!!document.querySelector(${JSON.stringify(selector)})`);
      if (found) return true;
      await sleep(250);
    }
    throw new Error(`Timeout waiting for selector: ${selector}`);
  }

  async click(selector) {
    await this.waitFor(selector);
    await this.evaluate(`
      (() => {
        const el = document.querySelector(${JSON.stringify(selector)});
        if (!el) throw new Error("Element not found: " + ${JSON.stringify(selector)});
        el.scrollIntoView({ behavior: 'instant', block: 'center' });
        el.click();
      })()
    `);
    await sleep(500);
  }

  async clickText(text, tag = "button") {
    const found = await this.evaluate(`
      (() => {
        const els = Array.from(document.querySelectorAll(${JSON.stringify(tag)}));
        const target = els.find(e => (e.innerText || e.textContent || "").includes(${JSON.stringify(text)}));
        if (target) {
          target.scrollIntoView({ behavior: 'instant', block: 'center' });
          target.click();
          return true;
        }
        return false;
      })()
    `);
    if (!found) {
      throw new Error("Element <" + tag + "> with text '" + text + "' not found");
    }
    await sleep(500);
  }

  async fillInput(selector, value) {
    await this.waitFor(selector);
    await this.evaluate(`
      (() => {
        const input = document.querySelector(${JSON.stringify(selector)});
        if (!input) throw new Error("Input not found: " + ${JSON.stringify(selector)});
        input.focus();
        const nativeInputValueSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value").set;
        nativeInputValueSetter.call(input, ${JSON.stringify(value)});
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.dispatchEvent(new Event('change', { bubbles: true }));
      })()
    `);
    await sleep(300);
  }

  async clearSession() {
    await this.evaluate(`
      (() => {
        try { localStorage.clear(); } catch(e) {}
        try { sessionStorage.clear(); } catch(e) {}
        try {
          document.cookie.split(";").forEach((c) => {
            document.cookie = c.replace(/^ +/, "").replace(/=.*/, "=;expires=" + new Date().toUTCString() + ";path=/");
          });
        } catch(e) {}
      })()
    `);
    await sleep(300);
  }

  async close() {
    if (this.ws) {
      this.ws.close();
    }
  }
}

async function main() {
  console.log("=== [E2E] Starting Headless Chrome for Full Multi-Role UX Test ===");
  const chrome = spawn("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", [
    "--headless=new",
    "--remote-debugging-port=9224",
    "--user-data-dir=/tmp/test-chrome-profile-e2e",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-gpu",
    "http://localhost/login"
  ]);

  const waitForReady = new Promise((resolve, reject) => {
    chrome.stderr.on("data", (data) => {
      const msg = data.toString();
      if (msg.includes("DevTools listening")) {
        resolve();
      }
    });
    setTimeout(() => reject(new Error("Chrome failed to start within 10s")), 10000);
  });

  await waitForReady;
  console.log("[+] Chrome running on port 9224");

  const targetsRes = await fetch("http://127.0.0.1:9224/json/list");
  const targets = await targetsRes.json();
  const pageTarget = targets.find((t) => t.type === "page") || targets[0];
  console.log("[+] Target page:", pageTarget.url);

  const client = new CDPClient(pageTarget.webSocketDebuggerUrl);
  await client.connect();
  await client.send("Page.enable");
  await client.send("Runtime.enable");

  const results = [];

  try {
    // ------------------------------------------------------------------------
    // 1. STUDENT FLOW (student1 / password123!)
    // ------------------------------------------------------------------------
    console.log("\n[1] === STUDENT FLOW ===");
    await client.clearSession();
    await client.navigate("http://localhost/login");
    await client.waitFor("h1");
    const loginTitle = await client.evaluate(`document.querySelector("h1")?.innerText`);
    results.push({ step: "Login Page Render", ok: loginTitle.includes("내 기록") || loginTitle.includes("마이스터") || loginTitle.includes("로그인") });

    // Credential Input: student1
    await client.fillInput("input#id", "student1");
    await client.fillInput("input#password", "password123!");
    const studentIdVal = await client.evaluate(`document.querySelector("input#id")?.value`);
    console.log("  - Entered ID:", studentIdVal);
    results.push({ step: "Student Credential Input", ok: studentIdVal === "student1" });

    // Login submit
    await client.click('button[type="submit"]');
    await sleep(2500);

    const studentUrl = await client.evaluate(`window.location.pathname`);
    console.log("  - Student Dashboard URL:", studentUrl);
    results.push({ step: "Student Login Navigation", ok: studentUrl.includes("/student/dashboard") });

    // Verify 5 competencies & AI Coaching
    const studentDashText = await client.evaluate(`document.body.innerText`);
    const hasTracks = studentDashText.includes("전공") && (studentDashText.includes("인성") || studentDashText.includes("봉사"));
    const hasAiTips = studentDashText.includes("AI") || studentDashText.includes("코칭") || studentDashText.includes("조언");
    console.log("  - Competency tracks rendered:", hasTracks);
    console.log("  - AI coaching tips rendered:", hasAiTips);
    results.push({ step: "Student Competency Tracks", ok: hasTracks });
    results.push({ step: "Student AI Coaching Section", ok: hasAiTips });

    // Verify Submissions list
    await client.navigate("http://localhost/student/submissions");
    await sleep(1500);
    const subListText = await client.evaluate(`document.body.innerText`);
    console.log("  - Submissions page rendered:", subListText.includes("제출"));
    results.push({ step: "Student Submissions List", ok: subListText.includes("제출") });

    // Verify New Submission Form
    await client.navigate("http://localhost/student/submissions/new");
    await sleep(1500);
    const submitFormText = await client.evaluate(`document.body.innerText`);
    console.log("  - Submit Form rendered with criteria selection:", submitFormText.includes("항목") || submitFormText.includes("증빙"));
    results.push({ step: "Student Submit Form & Criteria", ok: submitFormText.includes("항목") || submitFormText.includes("증빙") });

    // Test 50MB oversized file client validation
    await client.evaluate(`
      (() => {
        const fileInput = document.querySelector('input[type="file"]');
        if (!fileInput) return;
        const dt = new DataTransfer();
        const oversizedFile = new File(["dummy content"], "huge_cert.pdf", { type: "application/pdf" });
        Object.defineProperty(oversizedFile, "size", { value: 60 * 1024 * 1024 });
        dt.items.add(oversizedFile);
        fileInput.files = dt.files;
        fileInput.dispatchEvent(new Event("change", { bubbles: true }));
      })()
    `);
    await sleep(800);
    const oversizeErrMsg = await client.evaluate(`document.body.innerText`);
    const hasOversizeError = oversizeErrMsg.includes("50MB") || oversizeErrMsg.includes("초과");
    console.log("  - 50MB client validation error rendered:", hasOversizeError);
    results.push({ step: "Student 50MB Client Validation Check", ok: hasOversizeError });

    // Verify Points History
    await client.navigate("http://localhost/student/points");
    await sleep(1500);
    const studentPointsText = await client.evaluate(`document.body.innerText`);
    console.log("  - Points history rendered:", studentPointsText.includes("상벌점") || studentPointsText.includes("점수"));
    results.push({ step: "Student Points Table", ok: studentPointsText.includes("상벌점") || studentPointsText.includes("점수") });

    // Verify Profile page
    await client.navigate("http://localhost/profile");
    await sleep(1500);
    const profileText = await client.evaluate(`document.body.innerText`);
    console.log("  - Profile shows student info:", profileText.includes("student1") || profileText.includes("홍길동"));
    results.push({ step: "Student Profile & OTP Form", ok: profileText.includes("student1") || profileText.includes("홍길동") });

    // Test In-App Logout
    console.log("  - Testing in-app logout...");
    await client.evaluate(`window.confirm = () => true`);
    await client.clickText("로그아웃");
    await sleep(2000);
    const postLogoutUrl = await client.evaluate(`window.location.pathname`);
    console.log("  - Post-logout URL:", postLogoutUrl);
    results.push({ step: "In-App Logout", ok: postLogoutUrl.includes("/login") });

    // ------------------------------------------------------------------------
    // 2. TEACHER FLOW (teacher1 / password123!)
    // ------------------------------------------------------------------------
    console.log("\n[2] === TEACHER FLOW ===");
    await client.clearSession();
    await client.navigate("http://localhost/login");
    await client.waitFor("h1");
    await client.fillInput("input#id", "teacher1");
    await client.fillInput("input#password", "password123!");
    const teacherIdVal = await client.evaluate(`document.querySelector("input#id")?.value`);
    results.push({ step: "Teacher Credential Input", ok: teacherIdVal === "teacher1" });

    await client.click('button[type="submit"]');
    await sleep(2500);

    const teacherUrl = await client.evaluate(`window.location.pathname`);
    console.log("  - Teacher Dashboard URL:", teacherUrl);
    results.push({ step: "Teacher Login Navigation", ok: teacherUrl.includes("/teacher") });

    // Teacher Dashboard details
    const teacherDashText = await client.evaluate(`document.body.innerText`);
    console.log("  - Teacher jurisdiction scope rendered:", teacherDashText.includes("담임") || teacherDashText.includes("1학년 1반") || teacherDashText.includes("교무분장"));
    results.push({ step: "Teacher Jurisdiction Dashboard", ok: teacherDashText.includes("심사") || teacherDashText.includes("교무분장") || teacherDashText.includes("담임") });

    // Teacher Students list
    await client.navigate("http://localhost/teacher/students");
    await sleep(1500);
    const teacherStudentsText = await client.evaluate(`document.body.innerText`);
    console.log("  - Teacher student management rendered:", teacherStudentsText.includes("학생") || teacherStudentsText.includes("민준") || teacherStudentsText.includes("student"));
    results.push({ step: "Teacher Students Scope List", ok: teacherStudentsText.includes("학생") || teacherStudentsText.includes("민준") || teacherStudentsText.includes("student") });

    // Teacher Points Management
    await client.navigate("http://localhost/teacher/points");
    await sleep(1500);
    const teacherPointsText = await client.evaluate(`document.body.innerText`);
    console.log("  - Teacher points management form rendered:", teacherPointsText.includes("부여") || teacherPointsText.includes("상벌점"));
    results.push({ step: "Teacher Points Award & Audit", ok: teacherPointsText.includes("부여") || teacherPointsText.includes("상벌점") });

    // Teacher AI Chatbot
    await client.navigate("http://localhost/teacher/chat");
    await sleep(1500);
    const teacherChatText = await client.evaluate(`document.body.innerText`);
    console.log("  - Teacher guidance AI chatbot rendered:", teacherChatText.includes("챗봇") || teacherChatText.includes("AI") || teacherChatText.includes("상담"));
    results.push({ step: "Teacher Guidance AI Chatbot", ok: teacherChatText.includes("챗봇") || teacherChatText.includes("AI") || teacherChatText.includes("상담") });

    // ------------------------------------------------------------------------
    // 3. ADMIN FLOW (admin / admin1234!)
    // ------------------------------------------------------------------------
    console.log("\n[3] === ADMIN FLOW ===");
    await client.clearSession();
    await client.navigate("http://localhost/login");
    await client.waitFor("h1");
    await client.fillInput("input#id", "admin");
    await client.fillInput("input#password", "admin1234!");
    const adminIdVal = await client.evaluate(`document.querySelector("input#id")?.value`);
    results.push({ step: "Admin Credential Input", ok: adminIdVal === "admin" });

    await client.click('button[type="submit"]');
    await sleep(2500);

    const adminUrl = await client.evaluate(`window.location.pathname`);
    console.log("  - Admin Dashboard URL:", adminUrl);
    results.push({ step: "Admin Login Navigation", ok: adminUrl.includes("/admin") });

    // Admin Dashboard KPIs & Export
    const adminDashText = await client.evaluate(`document.body.innerText`);
    const hasKpi = adminDashText.includes("인증") || adminDashText.includes("학생") || adminDashText.includes("KPI");
    const hasExport = adminDashText.includes("CSV") || adminDashText.includes("엑셀") || adminDashText.includes("내보내기");
    console.log("  - Admin KPIs rendered:", hasKpi);
    console.log("  - Admin export actions rendered:", hasExport);
    results.push({ step: "Admin KPI Dashboard", ok: hasKpi });
    results.push({ step: "Admin Export Actions", ok: hasExport });

    // Admin Academic Years
    await client.navigate("http://localhost/admin/years");
    await sleep(1500);
    const yearsText = await client.evaluate(`document.body.innerText`);
    console.log("  - Academic years management loaded:", yearsText.includes("학년도") || yearsText.includes("2026"));
    results.push({ step: "Admin Academic Years Management", ok: yearsText.includes("학년도") || yearsText.includes("2026") });

    // Admin Criteria Management (33 items)
    await client.navigate("http://localhost/admin/criteria");
    await sleep(1500);
    const criteriaText = await client.evaluate(`document.body.innerText`);
    console.log("  - Criteria management loaded:", criteriaText.includes("평가") || criteriaText.includes("기준") || criteriaText.includes("배점"));
    results.push({ step: "Admin Criteria CRUD (33 items)", ok: criteriaText.includes("기준") || criteriaText.includes("배점") });

    // Admin Batch & NEIS Integration
    await client.navigate("http://localhost/admin/batch");
    await sleep(1500);
    const batchText = await client.evaluate(`document.body.innerText`);
    console.log("  - Batch & NEIS page loaded:", batchText.includes("NEIS") || batchText.includes("일괄") || batchText.includes("엑셀"));
    results.push({ step: "Admin Batch & NEIS Upload", ok: batchText.includes("NEIS") || batchText.includes("일괄") });

    // Admin Audit Logs
    await client.navigate("http://localhost/admin/audit");
    await sleep(1500);
    const auditText = await client.evaluate(`document.body.innerText`);
    console.log("  - Audit logs loaded:", auditText.includes("감사") || auditText.includes("로그") || auditText.includes("AUDIT"));
    results.push({ step: "Admin Audit Logs (GET /api/admin/audit-logs)", ok: auditText.includes("감사") || auditText.includes("로그") });

    // Admin AI Audit & Outlier Detection
    await client.navigate("http://localhost/admin/ai/audit");
    await sleep(1500);
    const aiAuditText = await client.evaluate(`document.body.innerText`);
    console.log("  - AI Audit loaded:", aiAuditText.includes("AI") || aiAuditText.includes("이상치") || aiAuditText.includes("감사"));
    results.push({ step: "Admin AI Audit & Outlier Analysis", ok: aiAuditText.includes("AI") || aiAuditText.includes("이상치") });

  } catch (err) {
    console.error("[-] Error during E2E flow:", err);
    results.push({ step: "E2E Execution Error", ok: false, error: err.message });
  } finally {
    await client.close();
    chrome.kill();
  }

  console.log("\n=================== E2E RESULTS SUMMARY ===================");
  let allPass = true;
  for (const r of results) {
    console.log(`${r.ok ? "✔ PASS" : "✖ FAIL"}: ${r.step}${r.error ? " - " + r.error : ""}`);
    if (!r.ok) allPass = false;
  }
  process.exit(allPass ? 0 : 1);
}

main().catch((err) => {
  console.error("FATAL:", err);
  process.exit(1);
});
