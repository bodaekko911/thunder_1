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
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Segoe UI', sans-serif;
            background: #0a0d18;
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .box {
            background: #0f1424;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 16px;
            padding: 40px;
            width: 360px;
        }
        h2 {
            color: #00ff9d;
            font-size: 24px;
            margin-bottom: 6px;
        }
        p {
            color: #445066;
            font-size: 13px;
            margin-bottom: 28px;
        }
        label {
            display: block;
            color: #8899bb;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 1px;
            text-transform: uppercase;
            margin-bottom: 6px;
        }
        input {
            width: 100%;
            padding: 12px;
            background: #151c30;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 10px;
            color: white;
            font-size: 14px;
            margin-bottom: 18px;
            outline: none;
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
    </style>
</head>
<body>
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
            window.location.href = "/home";
        }

        // Press Enter to login
        document.addEventListener("keydown", e => {
            if (e.key === "Enter") login();
        });
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
        "name": user.name
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