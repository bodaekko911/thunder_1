from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user import User
from app.core.security import hash_password, verify_password, create_access_token
from app.schemas.user import UserCreate, UserOut, UserLogin

router = APIRouter(tags=["Auth"])


@router.get("/", response_class=HTMLResponse)
def login_page():
    return """
<!DOCTYPE html>
<html>
<head>
    <title>ERP Login</title>
    <style>
        :root {
            --bg: #0a0d18;
            --card: #0f1424;
            --border: rgba(255,255,255,0.08);
            --text: #ffffff;
            --sub: #8899bb;
            --muted: #445066;
            --accent: #00ff9d;
        }
        body.light {
            --bg: #f4f5ef;
            --card: #eceee6;
            --border: rgba(0,0,0,0.08);
            --text: #1a1e14;
            --sub: #4a5040;
            --muted: #7b816f;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: var(--bg);
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--text);
        }
        .box {
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 16px;
            padding: 40px;
            width: 360px;
            position: relative;
        }
        h2 {
            color: var(--accent);
            font-size: 24px;
            margin-bottom: 6px;
        }
        p {
            color: var(--muted);
            font-size: 13px;
            margin-bottom: 28px;
        }
        label {
            display: block;
            color: var(--sub);
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
            text-transform: uppercase;
            margin-bottom: 6px;
        }
        input {
            width: 100%;
            padding: 12px;
            background: rgba(21,28,48,0.9);
            border: 1px solid var(--border);
            border-radius: 10px;
            color: var(--text);
            font-size: 14px;
            margin-bottom: 18px;
            outline: none;
        }
        body.light input {
            background: rgba(255,255,255,0.55);
        }
        input:focus {
            border-color: rgba(0,255,157,0.4);
        }
        button {
            width: 100%;
            padding: 14px;
            background: linear-gradient(135deg, #00ff9d, #00d4ff);
            border: none;
            border-radius: 10px;
            color: #021a10;
            font-size: 15px;
            font-weight: 800;
            cursor: pointer;
        }
        button:hover { filter: brightness(1.1); }
        #error {
            color: #ff4d6d;
            font-size: 13px;
            margin-top: 12px;
            text-align: center;
            display: none;
        }
        .mode-btn {
            position: fixed;
            top: 18px;
            right: 18px;
            width: 40px;
            height: 40px;
            border-radius: 10px;
            border: 1px solid var(--border);
            background: var(--card);
            color: var(--sub);
            font-size: 16px;
            cursor: pointer;
            transition: all .2s;
        }
        .mode-btn:hover {
            transform: scale(1.06);
        }
    </style>
</head>
<body>
    <button class="mode-btn" id="mode-btn" onclick="toggleMode()" title="Toggle color mode">🌙</button>
    <div class="box">
        <h2>Welcome Back</h2>
        <p>Sign in to your ERP system</p>

        <label>Email</label>
        <input id="email" type="email" placeholder="you@example.com">

        <label>Password</label>
        <input id="password" type="password" placeholder="••••••••">

        <button onclick="login()">Sign In</button>
        <div id="error">Wrong email or password</div>
    </div>

    <script>
        function setModeButton(isLight) {
            const btn = document.getElementById("mode-btn");
            if (btn) btn.innerText = isLight ? "☀️" : "🌙";
        }

        function toggleMode() {
            const isLight = document.body.classList.toggle("light");
            localStorage.setItem("colorMode", isLight ? "light" : "dark");
            setModeButton(isLight);
        }

        function initializeColorMode() {
            const isLight = localStorage.getItem("colorMode") === "light";
            document.body.classList.toggle("light", isLight);
            setModeButton(isLight);
        }

        async function login() {
            let res = await fetch("/auth/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    email: document.getElementById("email").value,
                    password: document.getElementById("password").value
                })
            });
            let data = await res.json();
            if (data.error) {
                document.getElementById("error").style.display = "block";
                return;
            }
            localStorage.setItem("token", data.access_token);
            localStorage.setItem("user_name", data.name);
            localStorage.setItem("user_role", data.role);
            localStorage.setItem("user_permissions", data.permissions || "");
            window.location.href = "/home";
        }

        // Press Enter to login
        document.addEventListener("keydown", e => {
            if (e.key === "Enter") login();
        });

        initializeColorMode();
    </script>
</body>
</html>
"""


@router.post("/auth/login")
def login(data: UserLogin, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == data.email).first()
    if not user or not verify_password(data.password, user.password):
        return {"error": "Invalid email or password"}
    if not user.is_active:
        return {"error": "Account is disabled"}
    token = create_access_token({"sub": str(user.id), "role": user.role})
    return {
        "access_token": token,
        "token_type": "bearer",
        "role": user.role,
        "name": user.name,
        "permissions": user.permissions or ""
    }


@router.post("/auth/register", response_model=UserOut, status_code=201)
def register(data: UserCreate, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == data.email).first():
        raise HTTPException(status_code=400, detail="Email already registered")
    user = User(
        name=data.name,
        email=data.email,
        password=hash_password(data.password),
        role=data.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
