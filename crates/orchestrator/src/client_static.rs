//! Static client serving at `/` (SPEC-003). EP-005 owns the real client;
//! this serves a placeholder page that proves the route and will be replaced.

pub fn index_html() -> String {
    r#"<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>VIHS</title></head>
<body>
<h1>VIHS — Virtual Interactive Human System</h1>
<p>Client placeholder (EP-005).</p>
</body>
</html>"#
        .to_string()
}
