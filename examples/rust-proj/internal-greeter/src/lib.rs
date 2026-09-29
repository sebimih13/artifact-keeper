/// Return a greeting supplied by the internal registry package.
pub fn greeting(name: &str) -> String {
    format!("Hello, {name}, from cargo-internal!")
}

#[cfg(test)]
mod tests {
    #[test]
    fn greets_the_requested_name() {
        assert_eq!(super::greeting("Rust"), "Hello, Rust, from cargo-internal!");
    }
}
