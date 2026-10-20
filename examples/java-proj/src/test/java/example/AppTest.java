package example;

import org.junit.Test;
import static org.junit.Assert.assertEquals;

public class AppTest {
    @Test public void combinesInternalAndExternalJars() {
        assertEquals("Hello, Java from Artifact Keeper!", App.message("java"));
    }
}
