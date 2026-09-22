package example;

import com.fasterxml.jackson.databind.ObjectMapper;

public class Main {
    public record Greeting(String message, int number) {}

    public static void main(String[] args) throws Exception {
        ObjectMapper mapper = new ObjectMapper();
        Greeting original = new Greeting("Hello from Artifact Keeper", 42);
        String json = mapper.writeValueAsString(original);
        Greeting restored = mapper.readValue(json, Greeting.class);
        if (!original.equals(restored)) {
            throw new IllegalStateException("Jackson round-trip mismatch");
        }
        System.out.println("Jackson version: " + mapper.version());
        System.out.println(json);
        System.out.println("JSON round-trip successful");
    }
}
