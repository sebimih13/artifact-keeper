package example;

import org.apache.commons.lang3.StringUtils;

public final class App {
    private App() {}
    public static String message(String name) {
        return Greeter.greet(StringUtils.capitalize(name));
    }
    public static void main(String[] args) {
        System.out.println(message(args.length == 0 ? "java" : args[0]));
    }
}
