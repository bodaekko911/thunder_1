PASSWORD_MIN_LENGTH = 8
PASSWORD_MAX_LENGTH = 200


def password_min_length_message(subject: str = "Password") -> str:
    return f"{subject} must be at least {PASSWORD_MIN_LENGTH} characters"


def password_must_change_message() -> str:
    return "New password must be different"


def validate_password_policy(password: str, *, subject: str = "Password") -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise ValueError(password_min_length_message(subject))
    if len(password) > PASSWORD_MAX_LENGTH:
        raise ValueError(f"{subject} must be at most {PASSWORD_MAX_LENGTH} characters")
    return password


def validate_password_change(old_password: str, new_password: str) -> None:
    if old_password == new_password:
        raise ValueError(password_must_change_message())
