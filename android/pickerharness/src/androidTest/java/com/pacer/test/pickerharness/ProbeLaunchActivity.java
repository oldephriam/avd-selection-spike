package com.pacer.test.pickerharness;

import android.app.Activity;
import android.content.ComponentName;
import android.content.Intent;
import android.os.Bundle;
import android.util.Log;
import android.widget.TextView;

public final class ProbeLaunchActivity extends Activity {
    private static final String PROBE_PACKAGE = "com.pacer.test.probe";
    private static final String PROBE_ACTIVITY = "com.pacer.test.probe.ProbeActivity";
    private static final int PROBE_REQUEST_CODE = 73;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView status = new TextView(this);
        status.setText("Selection test launcher");
        setContentView(status);

        if (!getIntent().getBooleanExtra("pacer_test_launch_probe", false)) {
            finish();
            return;
        }
        Intent probe = new Intent();
        probe.setClassName(PROBE_PACKAGE, PROBE_ACTIVITY);
        probe.putExtra("pacer_test_launch_picker", true);
        Log.i("PacerLaunchTrace", "ACTIVITY_LAUNCH_TRACE stage=before_probe_launch"
                + ";sourceComponent=" + getComponentName().flattenToString()
                + ";targetComponent=" + probe.getComponent().flattenToString()
                + ";taskId=" + getTaskId()
                + ";isTaskRoot=" + isTaskRoot()
                + ";callingActivity=" + callingActivityName()
                + ";requestCode=" + PROBE_REQUEST_CODE
                + ";launchIntentFlags=" + formatFlags(probe.getFlags())
                + ";incomingIntentFlags=" + formatFlags(getIntent().getFlags()));
        startActivityForResult(probe, PROBE_REQUEST_CODE);
    }

    private String callingActivityName() {
        ComponentName caller = getCallingActivity();
        return caller == null ? "none" : caller.flattenToString();
    }

    private static String formatFlags(int flags) {
        return String.format(java.util.Locale.ROOT, "0x%08x", flags);
    }
}
