package com.openvoicebridge.scoprobe;

/**
 * What the generated BenchSpec.build() calls, in the order of the spec (goal 104). BenchActivity makes native widgets
 * from these calls; the PC test of the template language implements it to list the fields. Plain Java.
 */
interface BenchUi {
    void header(String title, String headerTemplate);

    /** via: BenchTemplate.VIA_BENCHD | VIA_DIRECT, the modes in which the panel is shown (goal 105). */
    void panel(String id, String title, int via);

    void field(BenchTemplate.Field field);

    void actionRow();

    void action(BenchTemplate.Action action);

    void results();

    void feed(BenchTemplate.Feed feed);
}
