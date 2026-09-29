use serde::Serialize;

#[derive(Serialize)]
struct Report {
    greeting: String,
    registry: &'static str,
}

fn report() -> Report {
    Report {
        greeting: ak_example_greeter::greeting("Rust"),
        registry: "cargo (virtual)",
    }
}

fn main() {
    println!("{}", serde_json::to_string_pretty(&report()).unwrap());
}

#[cfg(test)]
mod tests {
    #[test]
    fn combines_internal_and_external_crates() {
        let value = serde_json::to_value(super::report()).unwrap();
        assert_eq!(value["greeting"], "Hello, Rust, from cargo-internal!");
        assert_eq!(value["registry"], "cargo (virtual)");
    }
}
